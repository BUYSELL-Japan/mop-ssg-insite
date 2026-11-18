import type { Language } from './language';
import type { RestaurantItem } from './dataFetcher';
import { localized, toSchemaOpeningHours, type RestaurantLang } from './restaurant';

export function generateRestaurantSchema(r: RestaurantItem, lang: RestaurantLang, pageUrl: string) {
  const l = localized(r, lang);
  const openingHours = toSchemaOpeningHours(r.opening_hours);

  return {
    '@context': 'https://schema.org',
    '@type': 'Restaurant',
    name: l.name,
    ...(r.title_ja && r.title_ja !== l.name && { alternateName: r.title_ja }),
    ...(l.description && { description: l.description }),
    url: pageUrl,
    ...(r.photo_url && { image: r.photo_url }),
    ...(l.address && {
      address: {
        '@type': 'PostalAddress',
        streetAddress: l.address,
        addressRegion: 'Okinawa',
        addressCountry: 'JP'
      }
    }),
    ...(r.phone_number && { telephone: r.phone_number }),
    ...(openingHours.length > 0 && { openingHours }),
    ...(r.price_range && { priceRange: r.price_range }),
    ...(r.genre && { servesCuisine: l.genre }),
    ...(r.lat != null && r.lng != null && {
      geo: { '@type': 'GeoCoordinates', latitude: r.lat, longitude: r.lng }
    })
  };
}

interface GuideData {
  title: string;
  title_zh_tw: string;
  description_en: string;
  description_zh_tw: string;
  photo_url?: string;
  address?: string;
  address_zh_tw?: string;
  category?: number;
  slug: string;
}

// Category mapping based on GeoJSON category numbers
const categoryMap = {
  en: {
    1: 'Tourist Attractions',
    2: 'Activity',
    3: 'Hotels',
    4: 'Restaurant',
    5: 'Beaches',
    6: 'Hospitals',
    9: 'Naha Airport'
  },
  'zh-tw': {
    1: '觀光景點',
    2: '活動/體驗',
    3: '飯店/酒店',
    4: '餐廳/美食',
    5: '海灘',
    6: '醫院',
    9: '那霸機場'
  }
};

export function getCategoryName(categoryNumber: number | undefined, language: Language): string {
  if (!categoryNumber) return language === 'en' ? 'Tourist Attractions' : '觀光景點';

  const langKey = language === 'en' ? 'en' : 'zh-tw';
  const map = categoryMap[langKey] as { [key: number]: string };
  return map[categoryNumber] || (language === 'en' ? 'Tourist Attractions' : '觀光景點');
}

function getSchemaTypeByCategory(categoryNumber: number | undefined): string {
  // GeoJSON stores category as a string ("4"), so normalize before the numeric switch
  const category = Number(categoryNumber) || 1;

  switch (category) {
    case 1:
      return 'TouristAttraction';
    case 3:
      return 'Hotel';
    case 4:
      return 'Restaurant';
    case 5:
      return 'Beach';
    case 6:
      return 'Hospital';
    case 9:
      return 'Airport';
    default:
      return 'Place';
  }
}

export function generateTouristAttractionSchema(
  guideData: GuideData,
  language: Language,
  pageUrl: string
) {
  const name = language === 'en' ? guideData.title : guideData.title_zh_tw;
  const description = language === 'en' ? guideData.description_en : guideData.description_zh_tw;
  const address = language === 'en' ? guideData.address : guideData.address_zh_tw;
  const schemaType = getSchemaTypeByCategory(guideData.category);

  return {
    '@context': 'https://schema.org',
    '@type': schemaType,
    name: name,
    description: description,
    image: guideData.photo_url || '',
    url: pageUrl,
    ...(address && {
      address: {
        '@type': 'PostalAddress',
        addressLocality: address.includes('那覇') || address.includes('Naha') ? 'Naha' : 'Okinawa',
        addressRegion: 'Okinawa',
        addressCountry: 'JP',
        streetAddress: address
      }
    })
  };
}

export function generateBreadcrumbSchema(
  categoryNumber: number | undefined,
  spotName: string,
  language: Language,
  baseUrl: string,
  spotSlug: string,
  section: 'spot' | 'restaurant' = 'spot'
) {
  const lang = language === 'en' || language === 'ko' ? language : 'zh';
  // Restaurants have no index page yet, so the home crumb points at the spot index (en/zh only)
  const homeUrl = `${baseUrl}/${lang === 'ko' ? 'en' : lang}/spot`;
  const spotUrl = `${baseUrl}/${lang}/${section}/${spotSlug}`;

  const homeName = language === 'ko' ? '홈' : language === 'en' ? 'Home' : '主頁';

  return {
    '@context': 'https://schema.org',
    '@type': 'BreadcrumbList',
    itemListElement: [
      {
        '@type': 'ListItem',
        position: 1,
        name: homeName,
        item: homeUrl
      },
      {
        '@type': 'ListItem',
        position: 2,
        name: spotName,
        item: spotUrl
      }
    ]
  };
}

export function generateWebPageSchema(
  guideData: GuideData,
  language: Language,
  pageUrl: string,
  baseUrl: string
) {
  const name = language === 'en' ? guideData.title : guideData.title_zh_tw;
  const description = language === 'en' ? guideData.description_en : guideData.description_zh_tw;

  return {
    '@context': 'https://schema.org',
    '@type': 'WebPage',
    name: name,
    description: description,
    url: pageUrl,
    inLanguage: language === 'en' ? 'en' : 'zh',
    isPartOf: {
      '@type': 'WebSite',
      name: 'MOP Okinawa Guide',
      url: baseUrl
    }
  };
}
