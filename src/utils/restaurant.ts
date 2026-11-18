import type { RestaurantItem } from './dataFetcher';
import { genreLabel } from '../lib/genreLabels';
import { municipalityLabel } from '../lib/municipalities';

export type RestaurantLang = 'en' | 'zh' | 'ko';

/**
 * Languages pages are generated for. ko fields (title_ko / address_ko / description_ko) fall
 * back to English when empty. /ko/restaurant* is routed to this project by the
 * mop-insite-proxy Worker (worker/src/index.js) and is in the MOP-Okinawa SW
 * navigateFallbackDenylist.
 */
export const RESTAURANT_LANGS: RestaurantLang[] = ['en', 'zh', 'ko'];

// genre code (data) -> display label: src/lib/genreLabels.ts (all codes in restaurants.geojson)
export { genreLabel };

const DESCRIPTION_MAX = { en: 155, zh: 110, ko: 110 } as const; // CJK characters are wider in results
const SITE_SUFFIX = '- MOP';

/**
 * meta / og description: "<description start> | <municipality> · <genre>".
 * Without a description: "<genre> · <municipality> - MOP". No postal code, no raw genre code.
 */
export function metaDescription(r: RestaurantItem, lang: RestaurantLang): string {
  const l = localized(r, lang);
  const city = municipalityLabel(r.address_ja || r.address, lang);
  const body = l.description.replace(/\s+/g, ' ').trim();
  if (!body) return `${[l.genre, city].filter(Boolean).join(' · ')} ${SITE_SUFFIX}`.trim();

  const place = [city, l.genre].filter(Boolean).join(' · ');
  const tail = place ? ` | ${place}` : '';
  const room = DESCRIPTION_MAX[lang] - tail.length;
  if (body.length <= room) return body + tail;
  // Too long: end at the last full sentence that fits; otherwise cut (at a word for English) with "…"
  const cut = body.slice(0, room - 1);
  const sentenceEnd = Math.max(cut.lastIndexOf('. '), cut.lastIndexOf('。')); // ko sentences end with ". "
  if (sentenceEnd > room * 0.5) return cut.slice(0, sentenceEnd + 1).trimEnd() + tail;
  const space = lang === 'en' ? cut.lastIndexOf(' ') : -1;
  return (space > room * 0.6 ? cut.slice(0, space) : cut).trimEnd() + '…' + tail;
}

/** True when the record has its own content in this language (no English fallback needed). */
export function isTranslated(r: RestaurantItem, lang: RestaurantLang): boolean {
  // Shop names usually stay in Japanese/English, so a Korean description is what makes the page Korean
  if (lang === 'ko') return Boolean(r.description_ko);
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
    // Translated name of this language (translate_names.py), else the Japanese name
    name: (lang === 'zh' ? r.name_zh : lang === 'ko' ? r.name_ko : r.name_en) || r.title || r.title_ja,
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
