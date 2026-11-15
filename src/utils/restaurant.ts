import type { RestaurantItem } from './dataFetcher';

export type RestaurantLang = 'en' | 'zh' | 'ko';

/**
 * Languages pages are generated for. 'ko' is supported by the template (falls back to
 * English when title_ko / description_ko are null) but is NOT generated yet. Before adding
 * it here: route /ko/restaurant* in worker/src/index.js, add /ko/restaurant to the
 * MOP-Okinawa SW navigateFallbackDenylist, and fill in Korean data.
 */
export const RESTAURANT_LANGS: RestaurantLang[] = ['en', 'zh'];

// genre code (data) -> display label. Unknown codes fall back to the code itself.
const GENRE_LABELS: Record<string, Record<RestaurantLang, string>> = {
  okinawa_soba: { en: 'Okinawa Soba', zh: '沖繩麵', ko: '오키나와 소바' },
  taco_rice: { en: 'Taco Rice', zh: '塔可飯', ko: '타코라이스' },
  izakaya: { en: 'Izakaya', zh: '居酒屋', ko: '이자카야' },
  seafood: { en: 'Seafood', zh: '海鮮', ko: '해산물' },
  cafe: { en: 'Cafe', zh: '咖啡廳', ko: '카페' },
};

export function genreLabel(code: string, lang: RestaurantLang): string {
  return GENRE_LABELS[code]?.[lang] ?? GENRE_LABELS[code]?.en ?? code;
}

/** True when the record has its own content in this language (no English fallback needed). */
export function isTranslated(r: RestaurantItem, lang: RestaurantLang): boolean {
  if (lang === 'ko') return Boolean(r.title_ko && r.description_ko);
  return true;
}

export function restaurantPath(lang: RestaurantLang, slug: string): string {
  return `/${lang}/restaurant/${slug}`;
}

export function localized(r: RestaurantItem, lang: RestaurantLang) {
  // Each field: language-specific value, then English, then Japanese.
  const pick = (zh: string, ko: string, en: string) =>
    (lang === 'zh' ? zh : lang === 'ko' ? ko : en) || en;
  return {
    name: pick(r.title_zh_tw, r.title_ko, r.title) || r.title_ja,
    address: pick(r.address_zh_tw, r.address_ko, r.address) || r.address_ja,
    hoursNote: pick(r.opening_hours_note_zh_tw, '', r.opening_hours_note_en),
    description: pick(r.description_zh_tw, r.description_ko, r.description_en),
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
