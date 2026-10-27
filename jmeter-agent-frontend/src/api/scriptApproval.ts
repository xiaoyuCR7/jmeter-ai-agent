import { h } from 'vue'
import { ElMessageBox } from 'element-plus'

export interface ScriptFinding {
  name: string
  kind: string
}

const english = () => {
  try { return localStorage.getItem('jmeter-agent-lang') === 'en' } catch { return false }
}

export const findingLabel = (item: ScriptFinding) => item.name === item.kind ? item.kind : `${item.name}（${item.kind}）`

// Local runs execute a plan's scripts with the user's own privileges, so the backend refuses
// them (409 scripts_need_approval) until the user has seen exactly what will run.
export async function confirmScripts(findings: ScriptFinding[]): Promise<boolean> {
  const en = english()
  const items = findings.slice(0, 15).map(item => h('li', findingLabel(item)))
  if (findings.length > 15) items.push(h('li', en ? `…and ${findings.length - 15} more` : `……另有 ${findings.length - 15} 项`))
  const message = h('div', [
    h('p', en
      ? 'This plan runs scripts or commands on this machine with your privileges. They can read your files and run programs:'
      : '这个计划会在本机以你的权限运行脚本或命令，它们可以读取你的文件、运行程序：'),
    h('ul', { style: 'margin: 8px 0; padding-left: 20px; max-height: 240px; overflow: auto' }, items),
    h('p', en
      ? 'Run it only if you trust where the plan came from. Container execution mode isolates such scripts.'
      : '只有确认计划来源可信时才运行。容器执行模式可以隔离这类脚本。'),
  ])
  try {
    await ElMessageBox.confirm(message, en ? 'Run scripts on this machine?' : '在本机运行脚本？', {
      type: 'warning',
      confirmButtonText: en ? 'Run anyway' : '仍然运行',
      cancelButtonText: en ? 'Cancel' : '取消',
    })
    return true
  } catch {
    return false
  }
}

export async function withScriptApproval<T>(send: (allowScripts: boolean) => Promise<T>): Promise<T> {
  try {
    return await send(false)
  } catch (error: any) {
    const detail = error?.response?.data?.detail
    if (error?.response?.status !== 409 || detail?.code !== 'scripts_need_approval') throw error
    if (!(await confirmScripts(detail.findings || []))) {
      const message = english() ? 'Run cancelled: scripts were not approved' : '已取消执行：未批准运行脚本'
      // Shaped like an API error, so callers show it the way they show any failed start.
      throw Object.assign(new Error(message), { response: { data: { detail: message } } })
    }
    return send(true)
  }
}
