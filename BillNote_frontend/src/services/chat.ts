import request from '@/utils/request'

export interface ChatMessage {
  role: 'user' | 'assistant'
  content: string
}

export interface ChatSource {
  text: string
  source_type: 'markdown' | 'transcript'
  section_title?: string
  start_time?: number
  end_time?: number
}

export interface AskResponse {
  answer: string
  sources: ChatSource[]
}

export type IndexStatus = 'idle' | 'indexing' | 'indexed' | 'failed'

export interface ChatStatusResponse {
  indexed: boolean
  status: IndexStatus
}

export const indexTask = async (taskId: string): Promise<void> => {
  return await request.post('/chat/index', { task_id: taskId })
}

export const askQuestion = async (data: {
  task_id: string
  question: string
  history: ChatMessage[]
  provider_id: string
  model_name: string
  signal?: AbortSignal
}): Promise<AskResponse> => {
  const { signal, ...payload } = data
  return await request.post('/chat/ask', payload, { timeout: 60000, signal })
}

export const getChatStatus = async (taskId: string): Promise<ChatStatusResponse> => {
  return await request.get(`/chat/status?task_id=${taskId}`)
}

export const reviseNote = async (data: {
  task_id: string
  instruction: string
  markdown: string
  selection?: string
  history: ChatMessage[]
  provider_id: string
  model_name: string
  signal?: AbortSignal
}): Promise<{ candidate_markdown: string; scope: 'full' | 'selection'; notes: string }> => {
  const { signal, ...payload } = data
  return await request.post('/chat/revise', payload, { timeout: 120000, signal })
}
