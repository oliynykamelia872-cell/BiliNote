import { useState, useEffect, useCallback, useMemo, useRef } from 'react'
import { Bubble } from '@ant-design/x'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { Loader2, Trash2, ChevronDown, ChevronUp, BookOpen, UserRound, Bot, Maximize2, Minimize2, Send, Square } from 'lucide-react'
import { Textarea } from '@/components/ui/textarea'
import { toast } from 'react-hot-toast'
import { useChatStore } from '@/store/chatStore'
import { useTaskStore } from '@/store/taskStore'
import { askQuestion, getChatStatus, indexTask, reviseNote, type ChatSource, type IndexStatus } from '@/services/chat'

type ChatMode = 'half' | 'full'

interface ChatPanelProps {
  taskId: string
  mode: ChatMode
  onModeChange: (mode: ChatMode) => void
}

function SourceBadges({ sources }: { sources: ChatSource[] }) {
  const [expanded, setExpanded] = useState(false)

  if (!sources || sources.length === 0) return null

  return (
    <div className="mt-1.5">
      <button
        onClick={() => setExpanded(!expanded)}
        className="flex items-center gap-1 text-xs text-neutral-400 hover:text-neutral-600"
      >
        <BookOpen className="h-3 w-3" />
        <span>引用来源 ({sources.length})</span>
        {expanded ? <ChevronUp className="h-3 w-3" /> : <ChevronDown className="h-3 w-3" />}
      </button>
      {expanded && (
        <div className="mt-1 flex flex-wrap gap-1">
          {sources.map((s, i) => (
            <Badge key={i} variant="outline" className="text-xs font-normal">
              {s.source_type === 'markdown'
                ? s.section_title || '笔记'
                : `${(s.start_time ?? 0).toFixed(0)}s ~ ${(s.end_time ?? 0).toFixed(0)}s`}
            </Badge>
          ))}
        </div>
      )}
    </div>
  )
}

export default function ChatPanel({ taskId, mode, onModeChange }: ChatPanelProps) {
  const [input, setInput] = useState('')
  const [loading, setLoading] = useState(false)
  const [indexStatus, setIndexStatus] = useState<IndexStatus | null>(null)
  const [panelMode, setPanelMode] = useState<'ask' | 'revise'>('ask')
  const [candidate, setCandidate] = useState<{ markdown: string; notes: string } | null>(null)
  const selectionRef = useRef('')
  const requestController = useRef<AbortController | null>(null)
  const [execution, setExecution] = useState<'ask' | 'revise' | null>(null)

  const messages = useChatStore(state => state.chatHistory[taskId]) ?? []
  const addMessage = useChatStore(state => state.addMessage)
  const clearChat = useChatStore(state => state.clearChat)
  const applyRevision = useTaskStore(state => state.applyRevision)

  useEffect(() => () => requestController.current?.abort(), [])

  useEffect(() => {
    const updateSelection = () => { selectionRef.current = window.getSelection()?.toString().trim() || '' }
    document.addEventListener('selectionchange', updateSelection)
    return () => document.removeEventListener('selectionchange', updateSelection)
  }, [])

  const currentTaskId = useTaskStore(state => state.currentTaskId)
  const tasks = useTaskStore(state => state.tasks)
  const currentTask = useMemo(
    () => tasks.find(t => t.id === currentTaskId) ?? null,
    [tasks, currentTaskId],
  )

  // 检查索引状态，未索引时自动触发，indexing 时轮询
  useEffect(() => {
    if (!taskId) return
    let cancelled = false
    let timer: ReturnType<typeof setTimeout> | null = null

    const poll = async () => {
      try {
        const res = await getChatStatus(taskId)
        if (cancelled) return
        setIndexStatus(res.status)

        if (res.status === 'idle') {
          // 未索引，触发后台索引
          await indexTask(taskId)
          if (!cancelled) setIndexStatus('indexing')
        }

        // indexing 状态持续轮询
        if (res.status === 'indexing' || res.status === 'idle') {
          timer = setTimeout(poll, 2000)
        }
      } catch {
        if (!cancelled) setIndexStatus('failed')
      }
    }

    poll()
    return () => {
      cancelled = true
      if (timer) clearTimeout(timer)
    }
  }, [taskId])

  const handleSend = useCallback(
    async (value: string) => {
      const question = value.trim()
      if (!question || loading) return

      const providerId = currentTask?.formData?.provider_id
      const modelName = currentTask?.formData?.model_name
      if (!providerId || !modelName) {
        toast.error('无法获取模型配置，请确认任务已完成')
        return
      }

      addMessage(taskId, { role: 'user', content: question })
      setInput('')
      setLoading(true)
      const controller = new AbortController()
      requestController.current = controller
      setExecution('ask')

      try {
        const history = messages.map(m => ({ role: m.role, content: m.content }))
        const res = await askQuestion({
          task_id: taskId,
          question,
          history,
          provider_id: providerId,
          model_name: modelName,
          signal: controller.signal,
        })
        addMessage(taskId, {
          role: 'assistant',
          content: res.answer,
          sources: res.sources,
        })
      } catch (error: any) {
        if (error?.code !== 'ERR_CANCELED' && error?.name !== 'CanceledError' && error?.name !== 'AbortError') {
          toast.error('问答请求失败')
        }
      } finally {
        if (requestController.current === controller) requestController.current = null
        setLoading(false)
        setExecution(null)
      }
    },
    [loading, taskId, currentTask, messages, addMessage],
  )

  const handleSubmit = useCallback(async () => {
    const value = input
    if (panelMode === 'ask') {
      await handleSend(value)
      return
    }
    const instruction = value.trim()
    const markdown = Array.isArray(currentTask?.markdown)
      ? currentTask.markdown[0]?.content || ''
      : currentTask?.markdown || ''
    if (!instruction || !markdown || loading) return
    if (!currentTask?.formData?.provider_id || !currentTask?.formData?.model_name) {
      toast.error('无法获取模型配置'); return
    }
    setInput(''); setLoading(true)
    const controller = new AbortController()
    requestController.current = controller
    setExecution('revise')
    const selection = selectionRef.current
    try {
      const result = await reviseNote({
        task_id: taskId,
        instruction,
        markdown,
        selection,
        history: messages.map(m => ({ role: m.role, content: m.content })),
        provider_id: currentTask.formData.provider_id,
        model_name: currentTask.formData.model_name,
        signal: controller.signal,
      })
      setCandidate({ markdown: result.candidate_markdown, notes: result.notes })
    } catch (error: any) {
      if (error?.code !== 'ERR_CANCELED' && error?.name !== 'CanceledError' && error?.name !== 'AbortError') toast.error('文稿修订失败')
    } finally {
      if (requestController.current === controller) requestController.current = null
      setLoading(false)
      setExecution(null)
    }
  }, [currentTask, handleSend, input, loading, messages, panelMode, taskId])

  const stopExecution = () => {
    requestController.current?.abort()
    requestController.current = null
    setLoading(false)
    setExecution(null)
    toast('已停止当前请求', { icon: '■' })
  }

  // 转换为 Bubble.List 的数据格式
  const bubbleItems = useMemo(() => {
    const items = messages.map((msg, i) => ({
      key: `msg-${i}`,
      role: msg.role === 'user' ? ('user' as const) : ('ai' as const),
      content: msg.content,
      footer:
        msg.role === 'assistant' && msg.sources ? (
          <SourceBadges sources={msg.sources} />
        ) : undefined,
    }))

    if (loading) {
      items.push({
        key: 'loading',
        role: 'ai' as const,
        content: '思考中...',
        loading: true,
      } as any)
    }

    return items
  }, [messages, loading])

  // Bubble 角色配置
  const roles = useMemo(
    () => ({
      user: {
        placement: 'end' as const,
        avatar: (
          <div className="flex h-7 w-7 items-center justify-center rounded-full bg-blue-500 text-white">
            <UserRound className="h-4 w-4" />
          </div>
        ),
        variant: 'filled' as const,
        styles: { content: { background: '#3b82f6', color: '#fff' } },
      },
      ai: {
        placement: 'start' as const,
        avatar: (
          <div className="flex h-7 w-7 items-center justify-center rounded-full bg-neutral-500 text-white">
            <Bot className="h-4 w-4" />
          </div>
        ),
        variant: 'outlined' as const,
        contentRender: (content: any) => (
          <div className="markdown-body prose prose-sm max-w-none prose-p:my-1 prose-li:my-0.5 prose-headings:my-2">
            <ReactMarkdown remarkPlugins={[remarkGfm]}>
              {typeof content === 'string' ? content : String(content)}
            </ReactMarkdown>
          </div>
        ),
      },
    }),
    [],
  )

  if (indexStatus === null || indexStatus === 'indexing' || indexStatus === 'idle') {
    return (
      <div className="flex h-full flex-col items-center justify-center gap-3 text-neutral-400">
        <Loader2 className="h-6 w-6 animate-spin" />
        <div className="text-center">
          <p className="text-sm font-medium">正在索引笔记内容...</p>
          <p className="mt-1 text-xs">首次使用需下载 Embedding 模型（约 80MB），请耐心等待</p>
        </div>
      </div>
    )
  }

  if (indexStatus === 'failed') {
    return (
      <div className="flex h-full flex-col items-center justify-center gap-2 text-neutral-400">
        <span className="text-sm">索引失败，请重试</span>
        <Button
          size="sm"
          variant="outline"
          onClick={async () => {
            setIndexStatus('indexing')
            try {
              await indexTask(taskId)
            } catch {
              toast.error('索引请求失败')
              setIndexStatus('failed')
            }
          }}
        >
          重新索引
        </Button>
      </div>
    )
  }

  return (
    <div className="flex h-full flex-col border-l">
      {/* 头部 */}
      <div className="flex items-center justify-between border-b px-3 py-2">
        <div className="flex gap-1">
          <Button variant={panelMode === 'ask' ? 'default' : 'ghost'} size="sm" onClick={() => setPanelMode('ask')}>AI 问答</Button>
          <Button variant={panelMode === 'revise' ? 'default' : 'ghost'} size="sm" onClick={() => setPanelMode('revise')}>润色改写</Button>
        </div>
        <div className="flex items-center gap-1">
          <Button
            variant="ghost"
            size="sm"
            className="h-7 px-2 text-neutral-400 hover:text-neutral-600"
            onClick={() => onModeChange(mode === 'half' ? 'full' : 'half')}
            title={mode === 'half' ? '全屏' : '半屏'}
          >
            {mode === 'half' ? (
              <Maximize2 className="h-3.5 w-3.5" />
            ) : (
              <Minimize2 className="h-3.5 w-3.5" />
            )}
          </Button>
          {messages.length > 0 && (
            <Button
              variant="ghost"
              size="sm"
              className="h-7 px-2 text-neutral-400 hover:text-red-500"
              onClick={() => clearChat(taskId)}
            >
              <Trash2 className="h-3.5 w-3.5" />
            </Button>
          )}
        </div>
      </div>

      {loading && (
        <div className="flex items-center justify-between border-b bg-neutral-50 px-3 py-2 text-xs text-neutral-600">
          <div className="flex items-center gap-2">
            <Loader2 className="h-3.5 w-3.5 animate-spin" />
            <span>{execution === 'revise' ? '正在生成文稿修订候选稿…' : '正在思考并整理回答…'}</span>
          </div>
          <Button variant="outline" size="sm" className="h-7 px-2" onClick={stopExecution}>
            <Square className="mr-1.5 h-3 w-3 fill-current" />停止
          </Button>
        </div>
      )}

      {/* 消息列表 */}
      <div className="flex-1 overflow-hidden">
        {panelMode === 'revise' && candidate && (
          <div className="flex h-full flex-col gap-2 overflow-auto p-3">
            <div className="text-xs text-neutral-500">{candidate.notes}</div>
            <div className="min-h-0 flex-1 overflow-auto rounded border bg-neutral-50 p-3 text-sm whitespace-pre-wrap">{candidate.markdown}</div>
            <div className="flex gap-2">
              <Button size="sm" onClick={() => { applyRevision(taskId, candidate.markdown, currentTask?.formData?.model_name); setCandidate(null); toast.success('修订已应用') }}>应用修订</Button>
              <Button size="sm" variant="outline" onClick={() => setCandidate(null)}>放弃</Button>
            </div>
          </div>
        )}
        {panelMode === 'ask' && (
          messages.length === 0 && !loading ? (
          <div className="flex h-full items-center justify-center text-center text-sm text-neutral-400">
            <div>
              <p>针对笔记内容提问</p>
              <p className="mt-1 text-xs">例如：这个视频的核心观点是什么？</p>
            </div>
          </div>
        ) : (
          <Bubble.List
            items={bubbleItems}
            role={roles}
            style={{ height: '100%' }}
          />
          )
        )}
      </div>

      {/* 输入区域 */}
      <div className="border-t px-3 py-2">
        <div className="flex items-end gap-2">
          <Textarea
            value={input}
            onChange={event => setInput(event.target.value)}
            onKeyDown={event => {
              if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) {
                event.preventDefault()
                void handleSubmit()
              }
            }}
            disabled={loading}
            rows={2}
            className="min-h-10 resize-none text-sm"
            placeholder={panelMode === 'ask' ? '输入你的问题...' : '输入修订要求；选中文字后可只修改选中内容'}
          />
          <Button
            type="button"
            size="icon"
            onClick={() => void handleSubmit()}
            disabled={loading || !input.trim()}
            title="发送"
          >
            <Send className="h-4 w-4" />
          </Button>
        </div>
      </div>
    </div>
  )
}
