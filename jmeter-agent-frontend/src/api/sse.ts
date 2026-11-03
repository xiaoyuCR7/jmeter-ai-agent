// Reads a text/event-stream response and hands each `data:` line's JSON to onEvent, in order.
export async function readEvents(response: Response, onEvent: (data: any) => void): Promise<void> {
  const reader = response.body?.getReader()
  if (!reader) throw new Error('No response body')
  const decoder = new TextDecoder()
  let buffer = ''
  while (true) {
    const { done, value } = await reader.read()
    buffer += decoder.decode(value, { stream: !done })
    const lines = buffer.split(/\r?\n/)
    buffer = done ? '' : lines.pop() || ''
    for (const line of lines) {
      if (!line.startsWith('data: ')) continue
      try {
        onEvent(JSON.parse(line.slice(6)))
      } catch {
        // A malformed line is skipped; the final event says how the request ended.
      }
    }
    if (done) return
  }
}
