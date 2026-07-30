// 网文题材 + 风格流派常量（基于2025中国网络文学蓝皮书与起点三江榜趋势）

// 题材分类（key 与后端 Book.genre 对齐）
export const GENRES: Record<string, string> = {
  fantasy: '玄幻',
  xianxia: '仙侠',
  urban: '都市',
  historical: '历史',
  scifi: '科幻',
  qihuan: '奇幻',
  lightnovel: '轻小说',
  game: '游戏',
  infinite: '诸天无限',
  mystery: '悬疑',
  wuxia: '武侠',
  romance: '言情',
  military: '军事',
  sports: '体育',
  horror: '恐怖',
  comedy: '喜剧',
  other: '其他',
};

// 长篇小说风格流派（多选，最多3种叠加）
export const NOVEL_STYLES: Record<string, string> = {
  shuang: '爽文流（强爽点、快节奏、升级打脸）',
  nue: '虐文流（情感虐心、命运波折）',
  tian: '甜文流（CP甜宠、轻松治愈）',
  system: '系统流（系统金手指、任务奖励）',
  wudi: '无敌流（主角无敌、碾压一切）',
  gou: '苟道流（稳健发育、韬光养晦）',
  changsheng: '长生流（修仙长生、岁月流转）',
  jiazu: '家族流（家族传承、代际接力）',
  chongsheng: '重生流（重生逆袭、弥补遗憾）',
  wuxian: '无限流（副本穿梭、诸天万界）',
  zhongtian: '种田流（经营发展、基建扩张）',
  heidark: '黑暗流（暗黑向、道德灰度）',
  zhiyu: '治愈流（温暖治愈、日常向）',
  xuanyi: '悬疑流（推理解谜、层层反转）',
  rexue: '热血流（少年热血、友情羁绊）',
};

// 短篇小说特有风格
export const SHORT_STORY_STYLES: Record<string, string> = {
  fanzhuan: '反转向（结局反转、意料之外）',
  danyuan: '单元剧（独立单元、短小精悍）',
  yingshi: '影视化（镜头语言、改编友好）',
  first_person: '第一人称（沉浸叙事、内心独白）',
  xuanyi: '悬疑流（推理解谜、层层反转）',
  zhiyu: '治愈流（温暖治愈、日常向）',
  nue: '虐文流（情感虐心、命运波折）',
  tian: '甜文流（CP甜宠、轻松治愈）',
};

// 根据类型获取可选风格
export function getStylesForType(bookType: string): Record<string, string> {
  return bookType === 'short_story' ? SHORT_STORY_STYLES : NOVEL_STYLES;
}

// 根据类型获取卷数范围
export function getVolumeRange(bookType: string): { min: number; max: number; default: number; perVolumeWords: string } {
  if (bookType === 'short_story') {
    return { min: 1, max: 3, default: 1, perVolumeWords: '每篇约3-5万字' };
  }
  return { min: 5, max: 30, default: 10, perVolumeWords: '每卷约12万字（约50章）' };
}
