// @ts-check
import { defineConfig } from 'astro/config';

// https://astro.build/config
export default defineConfig({
  site: 'https://mop-insite.com',
  base: '/',
  output: 'static',
  build: {
    format: 'file',
    assets: '_ssg_assets',
    inlineStylesheets: 'always',
    assetsPrefix: '/'
  },
  vite: {
    base: '/',
    build: {
      minify: 'esbuild',
      cssCodeSplit: false
    }
  }
});
