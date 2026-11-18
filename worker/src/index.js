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
       // a missing hashed file would otherwise come back as index.html with 200 (see 8.)
       return notFoundIfHtml(await proxyFetch(originalPath, MAIN_APP_URL, request));
    }

    // 5. サイトマップ / robots.txt (SSGの動的スクリプトを呼び出す)
    if (path === '/sitemap.xml' || path === '/robots.txt') {
      return proxyFetch(originalPath, SSG_URL, request);
    }

    // 5-1. Search Console 所有確認ファイル (MOP-SSG public/google*.html)。Pages は .html を拡張子なしへ 308 で
    // 転送するので、拡張子なしで取りに行き 200 をそのまま返す（Google はリダイレクトを嫌う）。2026-11-18 まではこの
    // パスが地図アプリの index.html を返しており、所有確認は DNS の TXT レコードで通っている。
    if (/^\/google[0-9a-f]+\.html$/.test(path)) {
      return proxyFetch(originalPath.replace(/\.html$/, ''), SSG_URL, request);
    }

    // 5-2. ガイドブック側のサイトマップ (Astro自動生成分を呼び出す)
    if (path === '/sitemap-index.xml' || path.startsWith('/sitemap-0.xml')) {
      return proxyFetch(originalPath, GUIDEBOOK_URL, request);
    }

    // 6. ガイドブック表示
    if (path.startsWith('/en/guidebook') || path.startsWith('/zh/guidebook') || path.startsWith('/ko/guidebook')) {
      return proxyFetch(originalPath, GUIDEBOOK_URL, request);
    }

    // 7. スポット詳細表示
    if (path.startsWith('/en/spot') || path.startsWith('/zh/spot')) {
      return proxyFetch(originalPath, SSG_URL, request);
    }

    // 7-2. 飲食店詳細表示 (SSG)
    if (path.startsWith('/en/restaurant') || path.startsWith('/zh/restaurant') || path.startsWith('/ko/restaurant')) {
      return proxyFetch(originalPath, SSG_URL, request);
    }

    // 8. その他すべて (メインアプリ)
    // The map app is a single page at "/" (login also returns to "/"). Pages answers every unknown path with
    // its index.html and 200, so a non-"/" path that comes back as HTML is a soft 404: keep the body (people
    // still land on the map) but send 404 so search engines drop it (2026-11-18). Files such as /sw.js,
    // /manifest.json or /favicon.ico come back as non-HTML and pass through unchanged.
    const response = await proxyFetch(originalPath, MAIN_APP_URL, request);
    return originalPath === '/' ? response : notFoundIfHtml(response);

  } catch (err) {
    return fetch(new Request(new URL(new URL(request.url).pathname, MAIN_APP_URL).toString(), request));
  }
}

/** Map-app responses only: Pages' index.html fallback (200 + HTML) for a path that is not "/" -> same body, 404 */
function notFoundIfHtml(response) {
  const isHtml = response.status === 200 && (response.headers.get('content-type') || '').startsWith('text/html');
  return isHtml ? new Response(response.body, { status: 404, statusText: 'Not Found', headers: response.headers }) : response;
}

async function proxyFetch(path, origin, originalRequest) {
  const targetUrl = new URL(path, origin);
  const newRequest = new Request(targetUrl.toString(), originalRequest);
  return fetch(newRequest);
}

addEventListener('fetch', event => {
  event.respondWith(handleRequest(event.request));
});
