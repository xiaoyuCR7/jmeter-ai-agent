import axios from 'axios'
import { ElMessageBox } from 'element-plus'
import { english } from './scriptApproval'

export const baseURL = import.meta.env.VITE_API_URL || '/api/v1'

export const tokenFrom = (text: string) => text.match(/token=([^&\s]+)/)?.[1] ?? text.trim()

const openSession = (token: string) => axios.post(`${baseURL}/session`, { token })

// The start link carries the access token once; the backend answers with a cookie this page
// cannot read. The token leaves the address bar first so it stays out of the browser history.
export async function bootstrapSession() {
  if (!location.hash.startsWith('#token=')) return
  const token = tokenFrom(location.hash)
  history.replaceState(null, '', location.pathname + location.search)
  await openSession(token).catch(() => undefined)  // a stale link falls through to requestAccess
}

let asking = false

// Reached when a request comes back without a valid session, e.g. in a new browser.
export async function requestAccess() {
  if (asking) return
  asking = true
  const en = english()
  try {
    const { value } = await ElMessageBox.prompt(
      en ? 'Open JMeter Agent from the start script, or paste the access link printed in the backend console.'
        : '请用启动脚本打开，或粘贴后端控制台打印的访问链接。',
      en ? 'Access link needed' : '需要访问链接',
      {
        confirmButtonText: en ? 'Continue' : '继续',
        cancelButtonText: en ? 'Cancel' : '取消',
        inputValidator: (text) => !!text?.trim() || (en ? 'Paste the link' : '请粘贴链接'),
        beforeClose: async (action, instance, done) => {
          if (action !== 'confirm') return done()
          try {
            await openSession(tokenFrom(instance.inputValue))
            done()
          } catch {
            instance.inputErrorMessage = en ? 'This link is not valid' : '链接无效'
            instance.editorErrorMessage = instance.inputErrorMessage
          }
        },
      })
    if (value) location.reload()
  } catch { /* dismissed: the page stays as it is */ } finally {
    asking = false
  }
}
