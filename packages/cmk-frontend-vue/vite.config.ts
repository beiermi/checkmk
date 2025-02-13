import { fileURLToPath, URL } from 'node:url'

import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// https://vitejs.dev/config/
export default defineConfig(({ command }) => {
  const resultBuild = {
    clearScreen: false,
    plugins: [
      vue({
        template: {
          transformAssetUrls: {
            // don't convert images to ESM imports
            // https://github.com/vitejs/vite-plugin-vue/blob/69397c2bc5924/packages/plugin-vue/README.md#asset-url-handling
            img: [],
            video: [],
            image: []
          }
        }
      })
    ],
    resolve: {
      alias: {
        '@': fileURLToPath(new URL('./src', import.meta.url))
      }
    },
    build: {
      sourcemap: true,
      rollupOptions: {
        output: {
          // the build process does not support dynamic filenames
          // so we fix it to the name we expect in the site
          // we could rename the file, but then the sourcemap comment would not match.
          entryFileNames: 'assets/vue_min.js'
        },
        external: [
          // treat all themes files as external to the build
          // so the build process will ignore them
          /themes\/.*/
        ]
      }
    }
  }
  if (command == 'build') {
    return resultBuild
  } else {
    console.log(command)
    // we are in serve mode here, supporting auto hot reload
    return {
      ...resultBuild,
      server: {
        strictPort: true,
        proxy: {
          // dev server proxies whole checkmk to inject js resources and support auto hot reloading
          '^(?!/cmk-frontend-vue-ahr)': 'http://localhost/'
        }
      },
      base: '/cmk-frontend-vue-ahr',
    }
  }
})
