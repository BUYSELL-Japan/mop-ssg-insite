import type { RestaurantItem } from './dataFetcher';

export type RestaurantLang = 'en' | 'zh';
export const RESTAURANT_LANGS: RestaurantLang[] = ['en', 'zh'];

// genre code (data) -> display label. Unknown codes fall back to the code itself.
const GENRE_LABELS: Record<string, { en: string; zh: string }> = {
  okinawa_soba: { en: 'Okinawa Soba', zh: '沖繩麵' },
  taco_rice: { en: 'Taco Rice', zh: '塔可飯' },
  izakaya: { en: 'Izakaya', zh: '居酒屋' },
  seafood: { en: 'Seafood', zh: '海鮮' },
  cafe: { en: 'Cafe', zh: '咖啡廳' },
};

export function genreLabel(code: string, lang: RestaurantLang): string {
  return GENRE_LABELS[code]?.[lang] ?? code;
}

export function restaurantPath(lang: RestaurantLang, slug: string): string {
  return `/${lang}/restaurant/${slug}`;
}

export function localized(r: RestaurantItem, lang: RestaurantLang) {
  return {
    name: (lang === 'zh' ? r.title_zh_tw : r.title) || r.title || r.title_ja,
    address: (lang === 'zh' ? r.address_zh_tw : r.address) || r.address_ja,
    hoursNote: lang === 'zh' ? r.opening_hours_note_zh_tw : r.opening_hours_note_en,
    description: lang === 'zh' ? r.description_zh_tw : r.description_en,
    genre: genreLabel(r.genre, lang),
  };
}

/**
 * OSM opening_hours -> schema.org openingHours strings.
 * "Tu-Su 11:00-15:00,17:00-21:00; Mo off" -> ["Tu-Su 11:00-15:00", "Tu-Su 17:00-21:00"]
 * Rules it cannot express (off, PH, comments) are dropped.
 */
export function toSchemaOpeningHours(osm: string): string[] {
  return osm
    .split(';')
    .map((rule) => rule.trim())
    .filter((rule) => rule && !/\b(off|closed)\b/i.test(rule))
    .flatMap((rule) => {
      const m = rule.match(/^([A-Za-z,\-]+)\s+(.+)$/);
      if (!m) return [];
      return m[2].split(',').map((range) => `${m[1]} ${range.trim()}`);
    });
}
