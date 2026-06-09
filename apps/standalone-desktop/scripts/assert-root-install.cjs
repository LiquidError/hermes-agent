"use strict"

const fs = require("fs")
const path = require("path")

// Standalone client: dependencies are installed locally (isolated from the
// repo workspace), so the app's own node_modules is the source of truth.
const root = path.resolve(__dirname, "..")

try {
  fs.accessSync(path.join(root, "node_modules", "vite", "package.json"))
} catch {
  console.error(`Run install first: cd ${root} && npm ci --no-workspaces --ignore-scripts`)
  process.exit(1)
}
