/* NoteForm.tsx ---------------------------------------------------- */
import {
  Form,
  FormControl,
  FormField,
  FormItem,
  FormLabel,
  FormMessage,
} from '@/components/ui/form.tsx'
import { useEffect,useState } from 'react'
import { FieldErrors, useForm, useWatch } from 'react-hook-form'
import { zodResolver } from '@hookform/resolvers/zod'
import { z } from 'zod'

import { FileText, Info, Loader2, Plus, Upload } from 'lucide-react'
import { Alert, AlertDescription } from '@/components/ui/alert.tsx'
import { convertDocument, generateNote } from '@/services/note.ts'
import { uploadFile } from '@/services/upload.ts'
import { useTaskStore } from '@/store/taskStore'
import { useModelStore } from '@/store/modelStore'
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from '@/components/ui/tooltip.tsx'
import { Checkbox } from '@/components/ui/checkbox.tsx'
import { ScrollArea } from '@/components/ui/scroll-area.tsx'
import { Button } from '@/components/ui/button.tsx'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select.tsx'
import { Input } from '@/components/ui/input.tsx'
import { Textarea } from '@/components/ui/textarea.tsx'
import { noteStyles, noteFormats, videoPlatforms } from '@/constant/note.ts'
import { getDefaultModel } from '@/services/model.ts'
import { useNavigate } from 'react-router-dom'
import toast from 'react-hot-toast'

/* -------------------- 校验 Schema -------------------- */
/** 用户粘贴的链接常缺协议头（如 bilibili.com/...），无任何 scheme 时自动补 https:// */
const withScheme = (url: string) => (/^[a-z][a-z0-9+.-]*:\/\//i.test(url) ? url : `https://${url}`)

const formSchema = z
  .object({
    mode: z.enum(['video', 'document']).default('video'),
    video_url: z.string().optional(),
    platform: z.string().nonempty('请选择平台'),
    quality: z.enum(['fast', 'medium', 'slow']),
    screenshot: z.boolean().optional(),
    link: z.boolean().optional(),
    model_name: z.string().optional(),
    format: z.array(z.string()).default([]),
    style: z.string().nonempty('请选择笔记生成风格'),
    extras: z.string().optional(),
    video_understanding: z.boolean().optional(),
    video_interval: z.coerce.number().min(1).max(30).default(6).optional(),
    grid_size: z
      .tuple([z.coerce.number().min(1).max(10), z.coerce.number().min(1).max(10)])
      .default([2, 2])
      .optional(),
    document_file: z.instanceof(File).optional(),
    ocr_mode: z.enum(['offline_first', 'offline_only', 'visual_fallback', 'off']).default('offline_first'),
  })
  .superRefine(({ mode, video_url, platform, document_file, model_name }, ctx) => {
    if (mode === 'document') {
      if (!document_file) {
        ctx.addIssue({ code: 'custom', message: '请选择要转换的文档', path: ['document_file'] })
      }
      return
    }
    if (!model_name) {
      ctx.addIssue({ code: 'custom', message: '请选择模型（或先在设置页配置默认模型）', path: ['model_name'] })
    }
    if (platform === 'local') {
      if (!video_url) {
        ctx.addIssue({ code: 'custom', message: '本地文件路径不能为空', path: ['video_url'] })
      }
    }
    else {
      if (!video_url) {
        ctx.addIssue({ code: 'custom', message: '视频链接不能为空', path: ['video_url'] })
      }
      else {
        try {
          const url = new URL(withScheme(video_url))
          if (!['http:', 'https:'].includes(url.protocol))
            throw new Error()
        }
        catch {
          ctx.addIssue({ code: 'custom', message: '请输入正确的视频链接', path: ['video_url'] })
        }
      }
    }
  })

export type NoteFormValues = z.infer<typeof formSchema>

/* 本地纯音频扩展名：上传音频文件时自动关闭截图 / 视频理解 */
const AUDIO_FILE_EXTENSIONS = ['mp3', 'm4a', 'm4b', 'aac', 'wav', 'flac', 'ogg', 'opus', 'wma', 'aiff', 'alac']
const isAudioFileName = (name: string) => {
  const ext = name.split('.').pop()?.toLowerCase() || ''
  return AUDIO_FILE_EXTENSIONS.includes(ext)
}

/* -------------------- 可复用子组件 -------------------- */
const SectionHeader = ({ title, tip }: { title: string; tip?: string }) => (
  <div className="my-3 flex items-center justify-between">
    <h2 className="block">{title}</h2>
    {tip && (
      <TooltipProvider>
        <Tooltip>
          <TooltipTrigger asChild>
            <Info className="hover:text-primary h-4 w-4 cursor-pointer text-neutral-400" />
          </TooltipTrigger>
          <TooltipContent className="text-xs">{tip}</TooltipContent>
        </Tooltip>
      </TooltipProvider>
    )}
  </div>
)

const CheckboxGroup = ({
  value = [],
  onChange,
  disabledMap,
}: {
  value?: string[]
  onChange: (v: string[]) => void
  disabledMap: Record<string, boolean>
}) => (
  <div className="flex flex-wrap space-x-1.5">
    {noteFormats.map(({ label, value: v }) => (
      <label key={v} className="flex items-center space-x-2">
        <Checkbox
          checked={value.includes(v)}
          disabled={disabledMap[v]}
          onCheckedChange={checked =>
            onChange(checked ? [...value, v] : value.filter(x => x !== v))
          }
        />
        <span>{label}</span>
      </label>
    ))}
  </div>
)

/* -------------------- 主组件 -------------------- */
const NoteForm = () => {
  const navigate = useNavigate();
  const [isUploading, setIsUploading] = useState(false)
  const [uploadSuccess, setUploadSuccess] = useState(false)
  const [documentName, setDocumentName] = useState('')
  /* ---- 全局状态 ---- */
  const { addPendingTask, currentTaskId, setCurrentTask, getCurrentTask, retryTask } =
    useTaskStore()
  const { loadEnabledModels, modelList, showFeatureHint, setShowFeatureHint } = useModelStore()
  const [defaultModel, setDefaultModelState] = useState<{ provider_id: string, model_name: string }>({
    provider_id: '',
    model_name: '',
  })

  /* ---- 表单 ---- */
  const form = useForm<NoteFormValues>({
    resolver: zodResolver(formSchema),
    defaultValues: {
      platform: 'bilibili',
      mode: 'video',
      quality: 'medium',
      model_name: '',
      style: 'minimal',
      video_interval: 6,
      grid_size: [2, 2],
      format: [],
    },
  })
  const currentTask = getCurrentTask()

  /* ---- 派生状态（只 watch 一次，提高性能） ---- */
  const platform = useWatch({ control: form.control, name: 'platform' }) as string
  const mode = useWatch({ control: form.control, name: 'mode' })
  const documentOcrMode = useWatch({ control: form.control, name: 'ocr_mode' })
  const videoUnderstandingEnabled = useWatch({ control: form.control, name: 'video_understanding' })
  const videoUrl = useWatch({ control: form.control, name: 'video_url' })
  const isLocalAudio = platform === 'local' && isAudioFileName(videoUrl || '')
  const editing = currentTask && currentTask.id

  const goModelAdd = () => {
    navigate("/settings/model");
  };
  /* ---- 副作用 ---- */
  useEffect(() => {
    loadEnabledModels()
    // 服务端默认模型（UI 设置页配置）作为表单默认，未配置则留空让用户选择
    getDefaultModel({ silent: true })
      .then((def: any) => {
        if (def?.provider_id && def?.model_name) {
          setDefaultModelState({ provider_id: def.provider_id, model_name: def.model_name })
          if (!currentTask)
            form.setValue('model_name', def.model_name)
        }
      })
      .catch(() => {})
    return
  }, [])
  useEffect(() => {
    if (!currentTask) return
    const { formData } = currentTask

    console.log('currentTask.formData.platform:', formData.platform)

    form.reset({
      platform: formData.platform || 'bilibili',
      mode: formData.mode || 'video',
      video_url: formData.video_url || '',
      model_name: formData.model_name || defaultModel.model_name || '',
      style: formData.style || 'minimal',
      quality: formData.quality || 'medium',
      extras: formData.extras || '',
      screenshot: formData.screenshot ?? false,
      link: formData.link ?? false,
      video_understanding: formData.video_understanding ?? false,
      video_interval: formData.video_interval ?? 6,
      grid_size: formData.grid_size ?? [2, 2],
      format: formData.format ?? [],
    })
  }, [
    // 当下面任意一个变了，就重新 reset
    currentTaskId,
    // modelList 用来兜底 model_name
    modelList.length,
    // 还要加上 formData 的各字段，或者直接 currentTask
    currentTask?.formData,
    defaultModel.model_name,
  ])

  /* 选中本地音频时，自动关闭截图与视频理解选项 */
  useEffect(() => {
    if (!isLocalAudio) return
    form.setValue('video_understanding', false)
    const formats = form.getValues('format') || []
    if (formats.includes('screenshot')) {
      form.setValue('format', formats.filter(v => v !== 'screenshot'))
    }
  }, [isLocalAudio, form])

  /* ---- 帮助函数 ---- */
  const isGenerating = () => !['SUCCESS', 'FAILED', undefined].includes(getCurrentTask()?.status)
  const generating = isGenerating()
  const handleFileUpload = async (file: File, cb: (url: string) => void) => {
    const formData = new FormData()
    formData.append('file', file)
    setIsUploading(true)
    setUploadSuccess(false)

    try {
  
      const  data  = await uploadFile(formData)
        cb(data.url)
        setUploadSuccess(true)
    } catch (err) {
      console.error('上传失败:', err)
      // message.error('上传失败，请重试')
    } finally {
      setIsUploading(false)
    }
  }

  const onSubmit = async (values: NoteFormValues) => {
    if (values.mode === 'document') {
      if (!values.document_file) return
      const documentData = new FormData()
      documentData.append('file', values.document_file)
      documentData.append('ocr_mode', values.ocr_mode)
      if (values.ocr_mode !== 'offline_only' && values.ocr_mode !== 'off' && values.model_name) {
        const matched = modelList.find(m => m.model_name === values.model_name)
        // 只有能在已登记模型列表中定位到供应商才携带模型参数，否则交由后端默认模型
        if (matched) {
          documentData.append('model_name', values.model_name)
          documentData.append('provider_id', matched.provider_id)
        }
      }
      try {
        const data = await convertDocument(documentData)
        addPendingTask(data.task_id, 'document', { ...values, document_name: values.document_file.name })
      } catch (error) {
        console.error('提交文档转换失败：', error)
      }
      return
    }
    const matched = modelList.find(m => m.model_name === values.model_name)
    const payload = {
      ...values,
      video_url:
        values.platform === 'local' ? values.video_url : withScheme(values.video_url || ''),
      ...(matched ? { provider_id: matched.provider_id } : {}),
      // 定位不到供应商时不携带模型参数，交由后端默认模型解析
      model_name: matched ? values.model_name : undefined,
      task_id: currentTaskId || '',
    } as NoteFormValues
    if (currentTaskId) {
      retryTask(currentTaskId, payload)
      return
    }

    // message.success('已提交任务')
    try {
      const data = await generateNote(payload)
      addPendingTask(data.task_id, values.platform, payload)
    } catch (e: any) {
      // 就绪门禁：本地转写模型还没下载好。后端返回 reason='transcriber_model_not_ready'，
      // 引导用户去「设置 → 音频转写配置」下载，而不是留一个静默失败的任务。
      if (e?.data?.reason === 'transcriber_model_not_ready') {
        const downloading = e?.data?.downloading
        toast.error(
          downloading
            ? '转写模型正在下载中，请稍候再提交'
            : '转写模型尚未下载，请先去「音频转写配置」页下载',
        )
        if (!downloading) navigate('/settings/transcriber')
        return
      }
      // 其余错误：axios 拦截器已经弹过 toast，这里只兜底不让 promise 变成未处理 rejection
      console.error('提交任务失败：', e)
    }
  }
  const onInvalid = (errors: FieldErrors<NoteFormValues>) => {
    console.warn('表单校验失败：', errors)
    // message.error('请完善所有必填项后再提交')
  }
  const handleCreateNew = () => {
    // 🔁 这里清空当前任务状态
    // 比如调用 resetCurrentTask() 或者 navigate 到一个新页面
    setCurrentTask(null)
  }
  const FormButton = () => {
    const label = generating ? '正在生成…' : editing ? '重新生成' : '生成笔记'

    return (
      <div className="flex gap-2">
        <Button
          type="submit"
          className={!editing ? 'w-full' : 'w-2/3' + ' bg-primary'}
          disabled={generating}
        >
          {generating && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
          {label}
        </Button>

        {editing && (
          <Button type="button" variant="outline" className="w-1/3" onClick={handleCreateNew}>
            <Plus className="mr-2 h-4 w-4" />
            新建笔记
          </Button>
        )}
      </div>
    )
  }

  /* -------------------- 渲染 -------------------- */
  return (
    <div className="h-full w-full">
      <Form {...form}>
        <form onSubmit={form.handleSubmit(onSubmit, onInvalid)} className="space-y-4">
          {/* 顶部按钮 */}
          <FormButton></FormButton>

          <div className="grid grid-cols-2 gap-2">
            <Button type="button" variant={mode === 'video' ? 'default' : 'outline'} onClick={() => form.setValue('mode', 'video')}>
              视频笔记
            </Button>
            <Button type="button" variant={mode === 'document' ? 'default' : 'outline'} onClick={() => form.setValue('mode', 'document')}>
              <FileText className="mr-2 h-4 w-4" />文档转换
            </Button>
          </div>

          {mode === 'document' ? (
            <>
              <SectionHeader title="转换文档" tip="支持 Office、PDF、EPUB、网页、文本、邮件、图片和 ZIP 等本地文件" />
              <FormField
                control={form.control}
                name="document_file"
                render={({ field }) => (
                  <FormItem>
                    <label className="hover:border-primary flex h-32 cursor-pointer flex-col items-center justify-center rounded-md border-2 border-dashed border-gray-300 transition-colors">
                      <Upload className="mb-2 h-5 w-5 text-neutral-500" />
                      <span className="text-sm text-neutral-600">{documentName || '选择或拖入本地文件'}</span>
                      <input
                        className="hidden"
                        type="file"
                        accept=".pdf,.doc,.docx,.ppt,.pptx,.xls,.xlsx,.epub,.html,.htm,.csv,.tsv,.json,.xml,.yaml,.yml,.txt,.md,.ipynb,.msg,.zip,image/*"
                        disabled={!!editing}
                        onChange={event => {
                          const file = event.target.files?.[0]
                          field.onChange(file)
                          setDocumentName(file?.name || '')
                        }}
                      />
                    </label>
                    <FormMessage />
                  </FormItem>
                )}
              />
              <FormField
                control={form.control}
                name="ocr_mode"
                render={({ field }) => (
                  <FormItem>
                    <SectionHeader title="OCR 策略" tip="默认使用本地 OCR，识别不足时可调用已配置的视觉模型" />
                    <Select value={field.value} onValueChange={field.onChange}>
                      <FormControl><SelectTrigger><SelectValue /></SelectTrigger></FormControl>
                      <SelectContent>
                        <SelectItem value="offline_first">离线优先，视觉模型兜底</SelectItem>
                        <SelectItem value="offline_only">仅离线 OCR</SelectItem>
                        <SelectItem value="visual_fallback">仅视觉模型 OCR</SelectItem>
                        <SelectItem value="off">不做 OCR</SelectItem>
                      </SelectContent>
                    </Select>
                  </FormItem>
                )}
              />
              <FormField
                control={form.control}
                name="model_name"
                render={({ field }) => (
                  <FormItem>
                    <SectionHeader title="视觉模型" tip="仅在离线 OCR 未识别到文字时作为兜底使用" />
                    <Select value={field.value || defaultModel.model_name || ''} onValueChange={field.onChange}>
                      <FormControl><SelectTrigger><SelectValue placeholder="不使用视觉模型" /></SelectTrigger></FormControl>
                      <SelectContent>
                        {modelList.map(model => <SelectItem key={model.id} value={model.model_name}>{model.model_name}</SelectItem>)}
                      </SelectContent>
                    </Select>
                  </FormItem>
                )}
              />
              <div className="flex items-center gap-2">
                <Checkbox
                  checked={documentOcrMode === 'offline_first' || documentOcrMode === 'visual_fallback'}
                  onCheckedChange={enabled => {
                    if (enabled) form.setValue('ocr_mode', 'offline_first')
                    else if (documentOcrMode === 'offline_first') form.setValue('ocr_mode', 'offline_only')
                    else if (documentOcrMode === 'visual_fallback') form.setValue('ocr_mode', 'off')
                  }}
                />
                <FormLabel>离线识别不足时使用视觉模型兜底</FormLabel>
              </div>
            </>
          ) : <>

          {/* 内容链接 & 平台 */}
          <SectionHeader title="内容链接" tip="支持 B 站、YouTube、抖音、快手、小红书，以及本地视频/音频文件" />
          <div className="flex gap-2">
            {/* 平台选择 */}

            <FormField
              control={form.control}
              name="platform"
              render={({ field }) => (
                <FormItem>
                  <Select
                    disabled={!!editing}
                    value={field.value}
                    onValueChange={field.onChange}
                    defaultValue={field.value}
                  >
                    <FormControl>
                      <SelectTrigger className="w-32">
                        <SelectValue />
                      </SelectTrigger>
                    </FormControl>
                    <SelectContent>
                      {videoPlatforms?.map(p => (
                        <SelectItem key={p.value} value={p.value}>
                          <div className="flex items-center justify-center gap-2">
                            <div className="h-4 w-4">{p.logo()}</div>
                            <span>{p.label}</span>
                          </div>
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                  <FormMessage style={{ display: 'none' }} />
                </FormItem>
              )}
            />
            {/* 链接输入 / 上传框 */}
            <FormField
              control={form.control}
              name="video_url"
              render={({ field }) => (
                <FormItem className="flex-1">
                  {platform === 'local' ? (
                    <>
                      <Input disabled={!!editing} placeholder="请输入本地视频/音频路径" {...field} />
                    </>
                  ) : (
                    <Input disabled={!!editing} placeholder="请输入视频网站链接" {...field} />
                  )}
                  <FormMessage style={{ display: 'none' }} />
                </FormItem>
              )}
            />
          </div>

          <FormField
            control={form.control}
            name="video_url"
            render={({ field }) => (
              <FormItem className="flex-1">
                {platform === 'local' && (
                  <>
                    <div
                      className="hover:border-primary mt-2 flex h-40 cursor-pointer items-center justify-center rounded-md border-2 border-dashed border-gray-300 transition-colors"
                      onDragOver={e => {
                        e.preventDefault()
                        e.stopPropagation()
                      }}
                      onDrop={e => {
                        e.preventDefault()
                        const file = e.dataTransfer.files?.[0]
                        if (file) handleFileUpload(file, field.onChange)
                      }}
                      onClick={() => {
                        const input = document.createElement('input')
                        input.type = 'file'
                        input.accept = 'video/*,audio/*,.mp3,.m4a,.m4b,.aac,.wav,.flac,.ogg,.opus,.wma'
                        input.onchange = e => {
                          const file = (e.target as HTMLInputElement).files?.[0]
                          if (file) handleFileUpload(file, field.onChange)
                        }
                        input.click()
                      }}
                    >
                      {isUploading ? (
                        <p className="text-center text-sm text-blue-500">上传中，请稍候…</p>
                      ) : uploadSuccess ? (
                        <p className="text-center text-sm text-green-500">上传成功！</p>
                      ) : (
                        <p className="text-center text-sm text-gray-500">
                          拖拽视频或音频文件到这里上传 <br />
                          <span className="text-xs text-gray-400">或点击选择文件，支持 MP3 / M4A / WAV 等音频</span>
                        </p>
                      )}
                    </div>
                  </>
                )}
                <FormMessage />
              </FormItem>
            )}
          />
          <div className="grid grid-cols-2 gap-2">
            {/* 模型选择 */}
            {

             modelList.length>0?(     <FormField
               className="w-full"
               control={form.control}
               name="model_name"
               render={({ field }) => (
                 <FormItem>
                   <SectionHeader title="模型选择" tip="不同模型效果不同，建议自行测试" />
                   <Select
                     onOpenChange={()=>{
                       loadEnabledModels()
                     }}
                     value={field.value}
                     onValueChange={field.onChange}
                     defaultValue={field.value}
                   >
                     <FormControl>
                       <SelectTrigger className="w-full min-w-0 truncate">
                         <SelectValue />
                       </SelectTrigger>
                     </FormControl>
                     <SelectContent>
                       {modelList.map(m => (
                         <SelectItem key={m.id} value={m.model_name}>
                           {m.model_name}
                         </SelectItem>
                       ))}
                     </SelectContent>
                   </Select>
                   <FormMessage />
                 </FormItem>
               )}
             />): (
               <FormItem>
                 <SectionHeader title="模型选择" tip="不同模型效果不同，建议自行测试" />
                  <Button type={'button'} variant={
                    'outline'
                  } onClick={()=>{goModelAdd()}}>请先添加模型</Button>
                 <FormMessage />
               </FormItem>
             )
            }

            {/* 笔记风格 */}
            <FormField
              className="w-full"
              control={form.control}
              name="style"
              render={({ field }) => (
                <FormItem>
                  <SectionHeader title="笔记风格" tip="选择生成笔记的呈现风格" />
                  <Select
                    value={field.value}
                    onValueChange={field.onChange}
                    defaultValue={field.value}
                  >
                    <FormControl>
                      <SelectTrigger className="w-full min-w-0 truncate">
                        <SelectValue />
                      </SelectTrigger>
                    </FormControl>
                    <SelectContent>
                      {noteStyles.map(({ label, value }) => (
                        <SelectItem key={value} value={value}>
                          {label}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                  <FormMessage />
                </FormItem>
              )}
            />
          </div>
          {/* 视频理解 */}
          <SectionHeader title="视频理解" tip="将视频截图发给多模态模型辅助分析" />
          <div className="flex flex-col gap-2">
            {isLocalAudio && (
              <Alert variant="warning" className="text-sm">
                <AlertDescription>
                  当前为本地音频文件，没有视频画面，截图与视频理解已自动关闭。
                </AlertDescription>
              </Alert>
            )}
            <FormField
              control={form.control}
              name="video_understanding"
              render={({ field }) => (
                <FormItem>
                  <div className="flex items-center gap-2">
                    <FormLabel>启用</FormLabel>
                    <Checkbox
                      checked={videoUnderstandingEnabled}
                      disabled={isLocalAudio}
                      onCheckedChange={v => form.setValue('video_understanding', v)}
                    />
                  </div>
                  <FormMessage />
                </FormItem>
              )}
            />

            <div className="grid grid-cols-2 gap-4">
              {/* 采样间隔 */}
              <FormField
                control={form.control}
                name="video_interval"
                render={({ field }) => (
                  <FormItem>
                    <FormLabel>采样间隔（秒）</FormLabel>
                    <Input disabled={!videoUnderstandingEnabled} type="number" {...field} />
                    <FormMessage />
                  </FormItem>
                )}
              />
              {/* 拼图大小 */}
              <FormField
                control={form.control}
                name="grid_size"
                render={({ field }) => (
                  <FormItem>
                    <FormLabel>拼图尺寸（列 × 行）</FormLabel>
                    <div className="flex items-center space-x-2">
                      <Input
                        disabled={!videoUnderstandingEnabled}
                        type="number"
                        value={field.value?.[0] || 3}
                        onChange={e => field.onChange([+e.target.value, field.value?.[1] || 3])}
                        className="w-16"
                      />
                      <span>x</span>
                      <Input
                        disabled={!videoUnderstandingEnabled}
                        type="number"
                        value={field.value?.[1] || 3}
                        onChange={e => field.onChange([field.value?.[0] || 3, +e.target.value])}
                        className="w-16"
                      />
                    </div>
                    <FormMessage />
                  </FormItem>
                )}
              />
            </div>
            <Alert variant="warning" className="text-sm">
              <AlertDescription>
                <strong>提示：</strong>视频理解功能必须使用多模态模型。
              </AlertDescription>
            </Alert>
          </div>

          {/* 笔记格式 */}
          <FormField
            control={form.control}
            name="format"
            render={({ field }) => (
              <FormItem>
                <SectionHeader title="笔记格式" tip="选择要包含的笔记元素" />
                <CheckboxGroup
                  value={field.value}
                  onChange={field.onChange}
                  disabledMap={{
                    link: platform === 'local',
                    screenshot: !videoUnderstandingEnabled || isLocalAudio,
                  }}
                />
                <FormMessage />
              </FormItem>
            )}
          />

          {/* 备注 */}
          <FormField
            control={form.control}
            name="extras"
            render={({ field }) => (
              <FormItem>
                <SectionHeader title="备注" tip="可在 Prompt 结尾附加自定义说明" />
                <Textarea placeholder="笔记需要罗列出 xxx 关键点…" {...field} />
                <FormMessage />
              </FormItem>
            )}
          />
          </>}
        </form>
      </Form>
    </div>
  )
}

export default NoteForm
