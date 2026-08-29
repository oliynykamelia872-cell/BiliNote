import request from '@/utils/request'

export interface ObsidianPath {
  id: string
  name: string
  path: string
  enabled: boolean
}

export const getObsidianPaths = (): Promise<ObsidianPath[]> =>
  request.get('/obsidian/paths', { suppressToast: true })
export const addObsidianPath = (data: Omit<ObsidianPath, 'id'>) => request.post('/obsidian/paths', data)
export const updateObsidianPath = (id: string, data: Omit<ObsidianPath, 'id'>) => request.put(`/obsidian/paths/${id}`, data)
export const deleteObsidianPath = (id: string) => request.delete(`/obsidian/paths/${id}`)
export const archiveToObsidian = (data: {
  task_id: string
  markdown: string
  title: string
  path_ids: string[]
  subfolder?: string
  tags?: string[]
  source_url?: string
  model?: string
  revision_id?: string
}) => request.post('/obsidian/archive', data, { timeout: 120000 })
