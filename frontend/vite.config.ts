import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import { fileURLToPath } from 'node:url'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173, strictPort: true,
    // Import only this catalog, never expose the project root or its .env.
    fs: { strict: true, allow: [fileURLToPath(new URL('.', import.meta.url)), fileURLToPath(new URL('../references/index.json', import.meta.url))] },
  },
})
