import { useEffect, useMemo, useState } from 'react'
import { Archive, X } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { archiveToObsidian, getObsidianPaths, type ObsidianPath } from '@/services/obsidian'
import toast from 'react-hot-toast'

interface Props {
  taskId: string
  markdown: string
  title: string
  source?: string
  model?: string
  revisionId?: string
  onClose: () => void
}

export default function ObsidianArchiveDialog({ taskId, markdown, title, source = '', model = '', revisionId = '', onClose }: Props) {
  const [paths, setPaths] = useState<ObsidianPath[]>([])
  const [selected, setSelected] = useState<string[]>([])
  const [subfolder, setSubfolder] = useState('')
  const [noteTitle, setNoteTitle] = useState(title)
  const [tags, setTags] = useState('AI, Codex')
  const [saving, setSaving] = useState(false)
  const [result, setResult] = useState<any[] | null>(null)

  useEffect(() => {
    getObsidianPaths().then(items => {
      setPaths(items)
      setSelected(items.filter(item => item.enabled).map(item => item.id))
    }).catch(() => toast.error('加载知识库路径失败'))
  }, [])

  const assetCount = useMemo(() => (markdown.match(/!\[[^\]]*\]\([^)]*\)/g) || []).length, [markdown])
  const submit = async () => {
    if (!selected.length) { toast.error('请至少选择一个知识库路径'); return }
    setSaving(true)
    try {
      const response = await archiveToObsidian({
        task_id: taskId, markdown, title: noteTitle, path_ids: selected,
        subfolder, tags: tags.split(',').map(item => item.trim()).filter(Boolean),
        source_url: source, model, revision_id: revisionId,
      })
      setResult(response.results)
      toast.success('文稿已转存到 Obsidian')
    } catch { /* interceptor displays the server error */ } finally { setSaving(false) }
  }
  return <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/30 p-4">
    <div className="max-h-[90vh] w-full max-w-xl overflow-auto rounded-lg bg-white p-5 shadow-xl">
      <div className="flex items-center justify-between"><h2 className="text-lg font-semibold">转存到 Obsidian</h2><Button variant="ghost" size="sm" onClick={onClose}><X className="h-4 w-4" /></Button></div>
      {!result ? <>
        <div className="mt-4 space-y-3">
          <Input value={noteTitle} onChange={e => setNoteTitle(e.target.value)} placeholder="笔记标题" />
          <Input value={subfolder} onChange={e => setSubfolder(e.target.value)} placeholder="子文件夹，例如 AI/Codex（可选）" />
          <Input value={tags} onChange={e => setTags(e.target.value)} placeholder="标签，用逗号分隔" />
        </div>
        <div className="mt-4 space-y-2"><div className="text-sm font-medium">选择知识库</div>{paths.length === 0 && <p className="text-sm text-neutral-500">暂无路径，请先在设置中添加。</p>}{paths.map(item => <label key={item.id} className="flex cursor-pointer items-start gap-2 rounded border p-2"><input type="checkbox" checked={selected.includes(item.id)} disabled={!item.enabled} onChange={e => setSelected(current => e.target.checked ? [...current, item.id] : current.filter(id => id !== item.id))} /><span className="min-w-0"><span className="block text-sm">{item.name}</span><span className="block truncate text-xs text-neutral-500">{item.path}</span></span></label>)}</div>
        <p className="mt-3 text-xs text-neutral-500">将复制约 {assetCount} 个图片附件；同名笔记会自动创建新版本。</p>
        <div className="mt-5 flex justify-end gap-2"><Button variant="outline" onClick={onClose}>取消</Button><Button onClick={submit} disabled={saving || !selected.length}><Archive className="mr-2 h-4 w-4" />{saving ? '转存中…' : '确认转存'}</Button></div>
      </> : <>
        <div className="mt-5 space-y-2">{result.map((item, index) => <div key={index} className="rounded border p-3 text-sm">{item.error ? <span className="text-red-600">失败：{item.error}</span> : <><div className="font-medium text-green-700">已保存</div><div className="break-all text-xs text-neutral-500">{item.path}</div>{item.warnings?.map((warning: string) => <div key={warning} className="text-xs text-amber-700">{warning}</div>)}</>}</div>)}</div>
        <div className="mt-5 flex justify-end"><Button onClick={onClose}>完成</Button></div>
      </>}
    </div>
  </div>
}
