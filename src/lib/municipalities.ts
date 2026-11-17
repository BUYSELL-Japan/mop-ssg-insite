/**
 * Okinawa's 41 municipalities: Japanese name -> display name (en / zh-Hant / ko), and extraction of
 * the municipality from a restaurant address. Used for meta descriptions (2026-11-18).
 * Addresses come in several shapes ("〒904-2215 沖縄県うるま市…", "沖縄県中頭郡北谷町…",
 * "21, 与那城上原 うるま市 沖縄県 904-2424"), so the name is searched anywhere in the address.
 */

import type { GenreLang } from './genreLabels';

type Labels = Record<GenreLang, string>;
const L = (en: string, zh: string, ko: string): Labels => ({ en, zh, ko });

export const MUNICIPALITIES: Record<string, Labels> = {
  那覇市: L('Naha', '那霸市', '나하시'),
  宜野湾市: L('Ginowan', '宜野灣市', '기노완시'),
  石垣市: L('Ishigaki', '石垣市', '이시가키시'),
  浦添市: L('Urasoe', '浦添市', '우라소에시'),
  名護市: L('Nago', '名護市', '나고시'),
  糸満市: L('Itoman', '絲滿市', '이토만시'),
  沖縄市: L('Okinawa City', '沖繩市', '오키나와시'),
  豊見城市: L('Tomigusuku', '豐見城市', '도미구스쿠시'),
  うるま市: L('Uruma', '宇流麻市', '우루마시'),
  宮古島市: L('Miyakojima', '宮古島市', '미야코지마시'),
  南城市: L('Nanjo', '南城市', '난조시'),
  国頭村: L('Kunigami', '國頭村', '구니가미손'),
  大宜味村: L('Ogimi', '大宜味村', '오기미손'),
  東村: L('Higashi', '東村', '히가시손'),
  今帰仁村: L('Nakijin', '今歸仁村', '나키진손'),
  本部町: L('Motobu', '本部町', '모토부초'),
  恩納村: L('Onna', '恩納村', '온나손'),
  宜野座村: L('Ginoza', '宜野座村', '기노자손'),
  金武町: L('Kin', '金武町', '긴초'),
  伊江村: L('Ie Island', '伊江村', '이에손'),
  読谷村: L('Yomitan', '讀谷村', '요미탄손'),
  嘉手納町: L('Kadena', '嘉手納町', '가데나초'),
  北谷町: L('Chatan', '北谷町', '차탄초'),
  北中城村: L('Kitanakagusuku', '北中城村', '기타나카구스쿠손'),
  中城村: L('Nakagusuku', '中城村', '나카구스쿠손'),
  西原町: L('Nishihara', '西原町', '니시하라초'),
  与那原町: L('Yonabaru', '與那原町', '요나바루초'),
  南風原町: L('Haebaru', '南風原町', '하에바루초'),
  渡嘉敷村: L('Tokashiki', '渡嘉敷村', '도카시키손'),
  座間味村: L('Zamami', '座間味村', '자마미손'),
  粟国村: L('Aguni', '粟國村', '아구니손'),
  渡名喜村: L('Tonaki', '渡名喜村', '도나키손'),
  南大東村: L('Minamidaito', '南大東村', '미나미다이토손'),
  北大東村: L('Kitadaito', '北大東村', '기타다이토손'),
  伊平屋村: L('Iheya', '伊平屋村', '이헤야손'),
  伊是名村: L('Izena', '伊是名村', '이제나손'),
  久米島町: L('Kumejima', '久米島町', '구메지마초'),
  八重瀬町: L('Yaese', '八重瀨町', '야에세초'),
  多良間村: L('Tarama', '多良間村', '다라마손'),
  竹富町: L('Taketomi', '竹富町', '다케토미초'),
  与那国町: L('Yonaguni', '與那國町', '요나구니초'),
};

// Longest names first so 北中城村 wins over 中城村 and 北大東村/南大東村 over 東村
const NAMES = Object.keys(MUNICIPALITIES).sort((a, b) => b.length - a.length);
const ALIASES: Record<string, string> = { 宮古市: '宮古島市' }; // typo seen in the source data

/** Japanese municipality name found in the address, or null. */
export function findMunicipality(address: string | null | undefined): string | null {
  if (!address) return null;
  const a = address.normalize('NFKC');
  let best: { name: string; at: number } | null = null;
  for (const name of NAMES) {
    let from = 0;
    for (;;) {
      const at = a.indexOf(name, from);
      if (at < 0) break;
      // 東村 inside 北大東村/南大東村, 中城村 inside 北中城村: skip when part of a longer name
      const longer = NAMES.some((n) => n.length > name.length && n.endsWith(name) && a.startsWith(n, at - (n.length - name.length)));
      if (!longer) {
        if (!best || at < best.at) best = { name, at };
        break;
      }
      from = at + name.length;
    }
  }
  if (best) return best.name;
  for (const [typo, name] of Object.entries(ALIASES)) if (a.includes(typo)) return name;
  return null;
}

/** Municipality display name in the page language, or '' when the address has none. */
export function municipalityLabel(address: string | null | undefined, lang: GenreLang): string {
  const name = findMunicipality(address);
  return name ? MUNICIPALITIES[name][lang] : '';
}
