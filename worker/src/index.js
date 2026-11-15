/**
 * MOP-Insite Proxy Worker v6 (サイトマップ完全対応版)
 */

const MAIN_APP_URL = 'https://mop-insite.pages.dev';
const GUIDEBOOK_URL = 'https://mop-guidebook-insite.pages.dev';
const SSG_URL = 'https://mop-ssg-insite.pages.dev';

async function handleRequest(request) {
  try {
    const url = new URL(request.url);
    const originalPath = url.pathname;
    const path = originalPath.toLowerCase();

    // 1. Service Worker 関連
    if (originalPath === '/sw.js' || originalPath.startsWith('/workbox-')) {
      return proxyFetch(originalPath, MAIN_APP_URL, request);
    }

    // 2. ガイドブック資産
    if (originalPath.startsWith('/_guidebook_assets/') || originalPath.startsWith('/_astro/')) {
      return proxyFetch(originalPath, GUIDEBOOK_URL, request);
    }

    // 3. SSG 資産
    if (originalPath.startsWith('/_ssg_assets/')) {
      return proxyFetch(originalPath, SSG_URL, request);
    }

    // 4. 一般的なアセットフォルダ
    if (originalPath.startsWith('/assets/')) {
       return proxyFetch(originalPath, MAIN_APP_URL, request);
    }

    // 5. サイトマップ / robots.txt (SSGの動的スクリプトを呼び出す)
    if (path === '/sitemap.xml' || path === '/robots.txt') {
      return proxyFetch(originalPath, SSG_URL, request);
    }

    // 5-2. ガイドブック側のサイトマップ (Astro自動生成分を呼び出す)
    if (path === '/sitemap-index.xml' || path.startsWith('/sitemap-0.xml')) {
      return proxyFetch(originalPath, GUIDEBOOK_URL, request);
    }

    // 6. ガイドブック表示
    if (path.startsWith('/en/guidebook') || path.startsWith('/zh/guidebook')) {
      return proxyFetch(originalPath, GUIDEBOOK_URL, request);
    }

    // 7. スポット詳細表示
    if (path.startsWith('/en/spot') || path.startsWith('/zh/spot')) {
      return proxyFetch(originalPath, SSG_URL, request);
    }

    // 7-2. 飲食店詳細表示 (SSG)
    if (path.startsWith('/en/restaurant') || path.startsWith('/zh/restaurant')) {
      return proxyFetch(originalPath, SSG_URL, request);
    }

    // 8. その他すべて (メインアプリ)
    return proxyFetch(originalPath, MAIN_APP_URL, request);

  } catch (err) {
    return fetch(new Request(new URL(new URL(request.url).pathname, MAIN_APP_URL).toString(), request));
  }
}

async function proxyFetch(path, origin, originalRequest) {
  const targetUrl = new URL(path, origin);
  const newRequest = new Request(targetUrl.toString(), originalRequest);
  return fetch(newRequest);
}

addEventListener('fetch', event => {
  event.respondWith(handleRequest(event.request));
});
