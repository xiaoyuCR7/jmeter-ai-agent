import { h } from 'vue'
import { ElMessageBox } from 'element-plus'

export interface ScriptFinding {
  name: string
  kind: string
}

export const english = () => {
  try { return localStorage.getItem('jmeter-agent-lang') === 'en' } catch { return false }
}

export const findingLabel = (item: ScriptFinding) => item.name === item.kind ? item.kind : `${item.name}（${item.kind}）`

// Local runs have the user's own privileges, so the backend refuses a plan with scripts or
// outside file access (409 scripts_need_approval) until the user has seen exactly what it does.
export async function confirmScripts(findings: ScriptFinding[]): Promise<boolean> {
  const en = english()
  const items = findings.slice(0, 15).map(item => h('li', findingLabel(item)))
  if (findings.length > 15) items.push(h('li', en ? `…and ${findings.length - 15} more` : `……另有 ${findings.length - 15} 项`))
  const message = h('div', [
    h('p', en
      ? 'This plan runs scripts or commands, or reaches files outside the app, on this machine with your privileges:'
      : '这个计划会在本机以你的权限运行脚本或命令，或访问应用目录之外的文件：'),
    h('ul', { style: 'margin: 8px 0; padding-left: 20px; max-height: 240px; overflow: auto' }, items),
    h('p', en
      ? 'Run it only if you trust where the plan came from. Container execution mode isolates it.'
      : '只有确认计划来源可信时才运行。容器执行模式可以隔离这些操作。'),
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

export async function withScriptApproval<T>(send: (approvedSha256?: string) => Promise<T>): Promise<T> {
  try {
    return await send()
  } catch (error: any) {
    const detail = error?.response?.data?.detail
    if (error?.response?.status !== 409 || detail?.code !== 'scripts_need_approval') throw error
    if (!(await confirmScripts(detail.findings || []))) {
      const message = english() ? 'Run cancelled: scripts were not approved' : '已取消执行：未批准运行脚本'
      // Shaped like an API error, so callers show it the way they show any failed start.
      throw Object.assign(new Error(message), { response: { data: { detail: message } } })
    }
    return send(detail.sha256)
  }
}
