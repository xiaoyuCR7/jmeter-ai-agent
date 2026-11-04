// Empties dist before a build. Vite does this itself with fs.rmSync, but on Windows Node 24's
// rmSync silently leaves files under a non-ASCII path (this project sits in 项目), so old hashed
// bundles piled up in the folder the backend serves. unlinkSync and rmdirSync do not have that bug.
import { existsSync, readdirSync, rmdirSync, statSync, unlinkSync } from 'node:fs'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'

function empty(dir) {
  for (const name of readdirSync(dir)) {
    const path = join(dir, name)
    if (statSync(path).isDirectory()) {
      empty(path)
      rmdirSync(path)
    } else {
      unlinkSync(path)
    }
  }
}

const dist = fileURLToPath(new URL('../dist', import.meta.url))
if (existsSync(dist)) empty(dist)
