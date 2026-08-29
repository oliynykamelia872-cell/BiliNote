import { useEffect, useRef, useState } from 'react'
import { FolderPlus, Trash2, Save, Pencil, X } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { getObsidianPaths, addObsidianPath, updateObsidianPath, deleteObsidianPath, type ObsidianPath } from '@/services/obsidian'
import toast from 'react-hot-toast'

export default function ObsidianPage() {
  const [paths, setPaths] = useState<ObsidianPath[]>([])
  const [name, setName] = useState('')
  const [path, setPath] = useState('')
  const [editingId, setEditingId] = useState<string | null>(null)
  const loadingPaths = useRef(false)
  const load = async (showError = true) => {
    if (loadingPaths.current) return
    loadingPaths.current = true
    try {
      for (const delay of [0, 500, 1200]) {
        if (delay) await new Promise(resolve => setTimeout(resolve, delay))
        try {
          setPaths(await getObsidianPaths())
          return
        } catch {
          continue
        }
      }
      if (showError) toast.error('加载知识库路径失败，请确认后端服务已启动')
    } finally {
      loadingPaths.current = false
    }
  }
  useEffect(() => { void load() }, [])

  const add = async () => {
    try {
      await addObsidianPath({ name, path, enabled: true })
      setName(''); setPath(''); await load(); toast.success('知识库路径已添加')
    } catch { /* interceptor displays the server error */ }
  }
  const edit = async () => {
    if (!editingId) return
    try {
      await updateObsidianPath(editingId, { name, path, enabled: true })
      setEditingId(null); setName(''); setPath(''); await load(); toast.success('知识库路径已更新')
    } catch { /* interceptor displays the server error */ }
  }
  const startEdit = (item: ObsidianPath) => { setEditingId(item.id); setName(item.name); setPath(item.path) }
  const cancelEdit = () => { setEditingId(null); setName(''); setPath('') }
  const toggle = async (item: ObsidianPath) => {
    try { await updateObsidianPath(item.id, { ...item, enabled: !item.enabled }); await load() } catch { /* displayed globally */ }
  }
  const chooseDirectory = async () => {
    if (!('__TAURI_INTERNALS__' in window)) return
    const { open } = await import('@tauri-apps/plugin-dialog')
    const selected = await open({ directory: true, multiple: false, title: '选择 Obsidian 知识库目录' })
    if (typeof selected === 'string') setPath(selected)
  }
  return <div className="h-full overflow-auto p-8">
    <h1 className="text-2xl font-semibold">Obsidian 知识库</h1>
    <p className="mt-2 text-sm text-neutral-500">配置可供文稿转存的本地知识库目录。</p>
    <div className="mt-6 max-w-2xl space-y-3 rounded-md border bg-white p-4">
      <input
        className="border-input placeholder:text-muted-foreground focus-visible:border-ring focus-visible:ring-ring/50 flex h-9 w-full min-w-0 rounded-md border bg-transparent px-3 py-1 text-base shadow-xs outline-none focus-visible:ring-[3px] md:text-sm"
        placeholder="路径名称，例如主知识库"
        value={name}
        onChange={event => setName(event.currentTarget.value)}
        onCompositionEnd={event => setName(event.currentTarget.value)}
        autoComplete="off"
      />
      <div className="flex gap-2"><Input className="flex-1" placeholder="知识库绝对路径" value={path} onChange={e => setPath(e.target.value)} /><Button type="button" variant="outline" onClick={chooseDirectory} disabled={!('__TAURI_INTERNALS__' in window)}>选择目录</Button></div>
      <div className="flex gap-2">
        <Button onClick={editingId ? edit : add} disabled={!name.trim() || !path.trim()}>{editingId ? <><Save className="mr-2 h-4 w-4" />保存修改</> : <><FolderPlus className="mr-2 h-4 w-4" />添加路径</>}</Button>
        {editingId && <Button type="button" variant="outline" onClick={cancelEdit}><X className="mr-2 h-4 w-4" />取消编辑</Button>}
      </div>
    </div>
    <div className="mt-6 max-w-2xl space-y-2">
      {paths.map(item => <div key={item.id} className="flex items-center gap-3 rounded-md border bg-white p-3">
        <input type="checkbox" checked={item.enabled} onChange={() => toggle(item)} />
        <div className="min-w-0 flex-1"><div className="font-medium">{item.name}</div><div className="truncate text-xs text-neutral-500">{item.path}</div></div>
        <Button variant="ghost" size="sm" onClick={() => startEdit(item)} title="编辑路径"><Pencil className="h-4 w-4" /></Button>
        <Button variant="ghost" size="sm" onClick={async () => { await deleteObsidianPath(item.id); await load() }}><Trash2 className="h-4 w-4" /></Button>
      </div>)}
    </div>
  </div>
}
