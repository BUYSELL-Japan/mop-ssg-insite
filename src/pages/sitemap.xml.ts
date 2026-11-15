import type { APIRoute } from 'astro';
import { fetchGuideData, fetchRestaurantData } from '../utils/dataFetcher';
import { RESTAURANT_LANGS, restaurantPath } from '../utils/restaurant';

interface SitemapEntry {
  url: string;
  lastmod: string;
  changefreq: string;
  priority: string;
}

const today = () => new Date().toISOString().split('T')[0];

// Each section builds its own entries so they can later be split into separate
// sitemap files (e.g. /sitemap-spots.xml, /sitemap-restaurants.xml) behind a sitemap index.
// Note: the Cloudflare Worker currently routes only /sitemap.xml to this project.

export async function buildSpotEntries(baseUrl: string): Promise<SitemapEntry[]> {
  const guideItems = await fetchGuideData();
  const lastmod = today();

  return [
    { url: `${baseUrl}/en/spot`, lastmod, changefreq: 'weekly', priority: '1.0' },
    { url: `${baseUrl}/zh/spot`, lastmod, changefreq: 'weekly', priority: '1.0' },
    ...guideItems.flatMap((item) => ['en', 'zh'].map((lang) => ({
      url: `${baseUrl}/${lang}/spot/${item.slug}`,
      lastmod,
      changefreq: 'monthly',
      priority: '0.8',
    }))),
  ];
}

export async function buildRestaurantEntries(baseUrl: string): Promise<SitemapEntry[]> {
  const restaurants = await fetchRestaurantData();

  return restaurants
    // Test records get a noindex page and are kept out of the sitemap
    .filter((r) => r.source !== 'test')
    .flatMap((r) => RESTAURANT_LANGS.map((lang) => ({
      url: `${baseUrl}${restaurantPath(lang, r.slug)}`,
      lastmod: /^\d{4}-\d{2}-\d{2}/.test(r.updated_at) ? r.updated_at.slice(0, 10) : today(),
      changefreq: 'monthly',
      priority: '0.6',
    })));
}

export function renderUrlset(entries: SitemapEntry[]): string {
  return `<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
${entries.map(({ url, lastmod, changefreq, priority }) => `  <url>
    <loc>${url}</loc>
    <lastmod>${lastmod}</lastmod>
    <changefreq>${changefreq}</changefreq>
    <priority>${priority}</priority>
  </url>`).join('\n')}
</urlset>`;
}

export const GET: APIRoute = async (context) => {
  const baseUrl = context.url.origin;

  try {
    const entries = [
      ...(await buildSpotEntries(baseUrl)),
      ...(await buildRestaurantEntries(baseUrl)),
    ];

    return new Response(renderUrlset(entries), {
      headers: {
        'Content-Type': 'application/xml; charset=utf-8',
        'Cache-Control': 'public, max-age=3600'
      }
    });
  } catch (error) {
    console.error('Error generating sitemap:', error);
    return new Response('Error generating sitemap', { status: 500 });
  }
};
