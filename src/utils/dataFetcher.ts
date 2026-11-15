export function slugify(text: string): string {
  return text
    .toString()
    .toLowerCase()
    .trim()
    .replace(/\s+/g, '-')
    .replace(/[^\w\-]+/g, '')
    .replace(/\-\-+/g, '-')
    .replace(/^-+/, '')
    .replace(/-+$/, '');
}

export async function fetchWithRetry(url: string, maxRetries: number = 3, delay: number = 1000): Promise<any> {
  let lastError: Error | null = null;

  for (let attempt = 1; attempt <= maxRetries; attempt++) {
    try {
      console.log(`Fetching data (attempt ${attempt}/${maxRetries}): ${url}`);

      const controller = new AbortController();
      const timeoutId = setTimeout(() => controller.abort(), 10000);

      const response = await fetch(url, {
        headers: {
          'User-Agent': 'Mozilla/5.0 (compatible; MOP-Guide/1.0)',
        },
        signal: controller.signal
      });

      clearTimeout(timeoutId);

      if (!response.ok) {
        throw new Error(`HTTP error! status: ${response.status} - ${response.statusText}`);
      }

      const data = await response.json();
      console.log(`Successfully fetched data on attempt ${attempt}`);
      return data;

    } catch (error) {
      lastError = error as Error;
      console.error(`Fetch attempt ${attempt} failed:`, error);

      if (attempt === maxRetries) {
        break;
      }

      const waitTime = delay * Math.pow(2, attempt - 1);
      console.log(`Waiting ${waitTime}ms before retry...`);
      await new Promise(resolve => setTimeout(resolve, waitTime));
    }
  }

  throw new Error(`Failed to fetch data after ${maxRetries} attempts. Last error: ${lastError?.message || 'Unknown error'}`);
}

interface GeoJSONFeature {
  type: string;
  properties: {
    id?: number | string;
    pin_id?: string;
    title?: string;
    title_zh_tw?: string;
    description_en?: string;
    description_zh_tw?: string;
    photo_url?: string;
    category?: number;
    address?: string;
    address_zh_tw?: string;
    [key: string]: any;
  };
  geometry: any;
}

interface GeoJSONData {
  type: string;
  features: GeoJSONFeature[];
}

export async function fetchGuideData() {
  try {
    const data = await fetchWithRetry('https://s3geojsonnew.s3.ap-southeast-2.amazonaws.com/Geojson3.23.geojson') as GeoJSONData;

    let guideItems: Array<{
      item_id: string;
      pin_id: string;
      slug: string;
      title: string;
      title_zh_tw: string;
      description_en: string;
      description_zh_tw: string;
      main_image_url?: string;
      photo_url?: string;
      category?: number;
      address?: string;
      address_zh_tw?: string;
    }> = [];

    if (data && data.type === 'FeatureCollection' && Array.isArray(data.features)) {
      guideItems = data.features
        .filter((feature: GeoJSONFeature) => feature && feature.properties && feature.properties.id)
        .map((feature: GeoJSONFeature) => {
          const title = feature.properties.title || '';
          return {
            item_id: String(feature.properties.id),
            pin_id: feature.properties.pin_id || String(feature.properties.id),
            slug: slugify(title),
            title: title,
            title_zh_tw: feature.properties.title_zh_tw || '',
            description_en: feature.properties.description_en || '',
            description_zh_tw: feature.properties.description_zh_tw || '',
            main_image_url: feature.properties.photo_url || undefined,
            photo_url: feature.properties.photo_url || undefined,
            category: feature.properties.category,
            address: feature.properties.address,
            // GeoJSON key is "address_zh-tw" (hyphen)
            address_zh_tw: feature.properties['address_zh-tw'] || feature.properties.address_zh_tw
          };
        });
    }

    return guideItems;
  } catch (error) {
    console.error('Failed to fetch guide data:', error);
    return [];
  }
}

// ---------------------------------------------------------------------------
// Restaurants (restaurants.geojson)
// ---------------------------------------------------------------------------

export const RESTAURANTS_GEOJSON_URL = 'https://s3geojsonnew.s3.ap-southeast-2.amazonaws.com/restaurants.geojson';

export interface RestaurantItem {
  pin_id: string;
  slug: string;
  title: string;
  title_ja: string;
  title_zh_tw: string;
  address: string;
  address_ja: string;
  address_zh_tw: string;
  phone_number: string;
  opening_hours: string;
  opening_hours_note_en: string;
  opening_hours_note_zh_tw: string;
  price_range: string;
  price_level: number | null;
  genre: string;
  lat: number | null;
  lng: number | null;
  show_on_map: boolean;
  official_url: string | null;
  description_en: string;
  description_zh_tw: string;
  photo_url: string | null;
  source: string;
  updated_at: string;
}

const str = (v: unknown): string => (typeof v === 'string' ? v : v == null ? '' : String(v));

let restaurantCache: Promise<RestaurantItem[]> | null = null;

/**
 * Fetches restaurants.geojson once per build (every route and the sitemap share the result).
 * Unlike fetchGuideData, a failed fetch throws so the build fails and the previous
 * deployment stays live, instead of silently publishing zero restaurant pages.
 */
export function fetchRestaurantData(): Promise<RestaurantItem[]> {
  if (!restaurantCache) {
    restaurantCache = fetchWithRetry(RESTAURANTS_GEOJSON_URL).then((data: any) => {
      if (!data || data.type !== 'FeatureCollection' || !Array.isArray(data.features)) {
        throw new Error('restaurants.geojson is not a FeatureCollection');
      }
      return data.features
        .filter((f: any) => f?.properties?.pin_id && f.properties.slug && str(f.properties.approved) === '1')
        .map((f: any): RestaurantItem => {
          const p = f.properties;
          // GeoJSON coordinates are [lng, lat]; geometry may be null until geocoded
          const coords = f.geometry?.type === 'Point' ? f.geometry.coordinates : null;
          return {
            pin_id: str(p.pin_id),
            slug: str(p.slug),
            title: str(p.title),
            title_ja: str(p.title_ja ?? p['title.ja']),
            title_zh_tw: str(p.title_zh_tw),
            address: str(p.address),
            address_ja: str(p.address_ja),
            address_zh_tw: str(p['address_zh-tw'] ?? p.address_zh_tw),
            phone_number: str(p.phone_number),
            opening_hours: str(p.opening_hours),
            opening_hours_note_en: str(p.opening_hours_note_en),
            opening_hours_note_zh_tw: str(p.opening_hours_note_zh_tw),
            price_range: str(p.price_range),
            price_level: Number.isInteger(p.price_level) ? p.price_level : null,
            genre: str(p.genre),
            lat: coords ? Number(coords[1]) : null,
            lng: coords ? Number(coords[0]) : null,
            show_on_map: p.show_on_map === true || p.show_on_map === 'true' || p.show_on_map === '1',
            official_url: p.official_url ? str(p.official_url) : null,
            description_en: str(p.description_en),
            description_zh_tw: str(p.description_zh_tw),
            photo_url: p.photo_url ? str(p.photo_url) : null,
            source: str(p.source),
            updated_at: str(p.updated_at),
          };
        });
    });
  }
  return restaurantCache;
}
