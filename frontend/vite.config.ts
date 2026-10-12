import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import {readFileSync} from 'node:fs'

const version=readFileSync(new URL('../VERSION.txt',import.meta.url),'utf8').trim().replace(/^\uFEFF/,'')
if(!/^\d+\.\d+\.\d+$/.test(version))throw new Error('VERSION.txt 需要使用 X.Y.Z 格式的版本号')
export default defineConfig({plugins:[react()],define:{__APP_VERSION__:JSON.stringify(version)},server:{proxy:{'/api':'http://127.0.0.1:8000'}},build:{chunkSizeWarningLimit:850}})
