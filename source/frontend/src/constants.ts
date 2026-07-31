// 网文题材 + 风格流派常量
// 数据来源：番茄小说/起点中文网/七猫小说/晋江文学城/知乎盐选等主流平台分类页调研整理
// 2026-07 维护

// ============================================================
// 第一部分：题材分类（key 与后端 Book.genre 对齐）
// ============================================================
// 说明：男频/女频题材在 WorkbenchPage 表单里按 optgroup 分组呈现，
// 这里合并为一张表方便统一查询。key 命名沿用项目既有约定。

export const GENRES: Record<string, string> = {
  // 通用
  other: '其他',
  // 男频
  fantasy: '玄幻',
  xianxia: '仙侠',
  qihuan: '奇幻',
  wuxia: '武侠',
  urban: '都市',
  urban_business: '都市职场',
  urban_fantasy: '都市异能',
  history: '历史',
  military: '军事',
  game: '游戏',
  sports: '体育',
  scifi: '科幻',
  mystery: '悬疑',
  infinite: '诸天无限',
  light_novel: '轻小说',
  // 女频
  romance: '现代言情',
  ancient_romance: '古代言情',
  fantasy_romance: '幻想言情',
  danmei: '纯爱',
  acg: '二次元',
};

// ============================================================
// 第二部分：题材 → 风格流派映射（长篇）
// ============================================================
// 每个题材对应一组该题材下常见的"子类型/流派"标签，供用户多选（最多3种叠加）。
// key 用拼音/英文短码，value 用网文圈通用的中文叫法。
// 数据综合起点二级分类 + 番茄/七猫标签 + 网文圈流派通行叫法。

interface StyleItem { key: string; label: string; }

const NOVEL_GENRE_STYLES: Record<string, StyleItem[]> = {
  // 男频 ——
  fantasy: [
    { key: 'dongfang', label: '东方玄幻' },
    { key: 'yishi', label: '异世大陆' },
    { key: 'gaowu', label: '高武世界' },
    { key: 'wangchao', label: '王朝争霸' },
    { key: 'honghuang', label: '洪荒流' },
    { key: 'fanren', label: '凡人流' },
    { key: 'feichai', label: '废柴逆袭流' },
    { key: 'qiangzhong', label: '强者重生流' },
    { key: 'qiandao', label: '签到流' },
    { key: 'shenchong', label: '神宠流' },
    { key: 'dihua', label: '迪化流' },
    { key: 'shenshu', label: '全球神祇流' },
  ],
  xianxia: [
    { key: 'gudian', label: '古典仙侠' },
    { key: 'xiuzhen', label: '修真文明' },
    { key: 'huanxiang', label: '幻想修仙' },
    { key: 'xiandai', label: '现代修真' },
    { key: 'shenhua', label: '神话修真' },
    { key: 'fengshen', label: '洪荒封神' },
    { key: 'goudao', label: '苟道流' },
    { key: 'changsheng', label: '长生流' },
    { key: 'jiazu', label: '家族修仙流' },
    { key: 'jianxiu', label: '剑修流' },
    { key: 'liandan', label: '炼丹流' },
    { key: 'liangi', label: '炼器流' },
  ],
  qihuan: [
    { key: 'jianmo', label: '剑与魔法' },
    { key: 'shishi', label: '史诗奇幻' },
    { key: 'shenmi', label: '神秘幻想' },
    { key: 'xiandai', label: '现代魔法' },
    { key: 'lishi', label: '历史神话' },
    { key: 'xifang', label: '西方奇幻' },
    { key: 'wushi', label: '巫师流' },
    { key: 'lingzhu', label: '领主贵族' },
    { key: 'mofa', label: '魔法校园' },
    { key: 'zhongshi', label: '中式奇幻' },
  ],
  wuxia: [
    { key: 'chuantong', label: '传统武侠' },
    { key: 'wuxia_fs', label: '武侠幻想' },
    { key: 'guoshu', label: '国术流' },
    { key: 'xinpa', label: '新派武侠' },
    { key: 'lishi_wx', label: '历史武侠' },
    { key: 'langzi', label: '浪子异侠' },
    { key: 'kuaiyi', label: '快意江湖' },
    { key: 'jianghu', label: '江湖恩怨' },
  ],
  urban: [
    { key: 'shenghuo', label: '都市生活' },
    { key: 'yishu', label: '异术超能' },
    { key: 'qingchun', label: '青春校园' },
    { key: 'mingxing', label: '娱乐明星' },
    { key: 'shangzhan', label: '商战职场' },
    { key: 'guanchang', label: '官场沉浮' },
    { key: 'dushi_xz', label: '都市修真' },
    { key: 'dushi_gw', label: '都市高武' },
    { key: 'shenyi', label: '神医流' },
    { key: 'shenhao', label: '神豪流' },
    { key: 'jianbao', label: '鉴宝流' },
    { key: 'bingwang', label: '兵王回归' },
    { key: 'cunzhi', label: '乡村种田' },
  ],
  urban_business: [
    { key: 'shangzhan', label: '商战职场' },
    { key: 'chuangye', label: '创业逆袭' },
    { key: 'zhichang', label: '职场权谋' },
    { key: 'shangye', label: '商业帝国' },
    { key: 'zhulian', label: '珠联璧合' },
    { key: 'jindiao', label: '金融大鳄' },
  ],
  urban_fantasy: [
    { key: 'yishu', label: '异术超能' },
    { key: 'dushi_xz', label: '都市修真' },
    { key: 'dushi_gw', label: '都市高武' },
    { key: 'guidze', label: '规则怪谈' },
    { key: 'dushi_nr', label: '都市脑洞' },
    { key: 'lingyi', label: '灵异民俗' },
  ],
  history: [
    { key: 'jiakong', label: '架空历史' },
    { key: 'qhsg', label: '秦汉三国' },
    { key: 'tangsong', label: '两晋隋唐' },
    { key: 'wudai', label: '五代十国' },
    { key: 'songming', label: '两宋元明' },
    { key: 'qingmg', label: '清史民国' },
    { key: 'chuanyue_ls', label: '穿越历史' },
    { key: 'keju', label: '科举入仕' },
    { key: 'zhongtian_ls', label: '历史种田' },
    { key: 'quanmou', label: '权谋庙堂' },
  ],
  military: [
    { key: 'junlv', label: '军旅生涯' },
    { key: 'zhanzheng', label: '军事战争' },
    { key: 'kangzhan', label: '抗战烽火' },
    { key: 'diedz', label: '谍战特工' },
    { key: 'tezhong', label: '特种军旅' },
    { key: 'xiandai_zz', label: '现代战争' },
    { key: 'chuanyue_zz', label: '穿越战争' },
  ],
  game: [
    { key: 'djj', label: '电子竞技' },
    { key: 'wlw', label: '虚拟网游' },
    { key: 'youxi_yj', label: '游戏异界' },
    { key: 'youxi_xt', label: '游戏系统' },
    { key: 'quantxi', label: '全息网游' },
    { key: 'disitianzai', label: '第四天灾流' },
    { key: 'shuju', label: '数据流' },
    { key: 'zhandui', label: '战队夺冠' },
  ],
  sports: [
    { key: 'zuqiu', label: '足球运动' },
    { key: 'lanqiu', label: '篮球运动' },
    { key: 'wangqiu', label: '网球/乒乓球' },
    { key: 'zonghe_ty', label: '综合竞技' },
    { key: 'dianjing_ty', label: '电竞体育' },
    { key: 'rexue_ty', label: '热血竞技' },
    { key: 'xiaoyuan_ty', label: '体育校园' },
  ],
  scifi: [
    { key: 'xingji', label: '星际文明' },
    { key: 'weilai', label: '未来世界' },
    { key: 'chaojikj', label: '超级科技' },
    { key: 'shikong', label: '时空穿梭' },
    { key: 'jinhua', label: '进化变异' },
    { key: 'moshi', label: '末世危机' },
    { key: 'jijia', label: '古武机甲' },
    { key: 'saibo', label: '赛博朋克' },
    { key: 'feitu', label: '废土生存' },
    { key: 'xingji_zz', label: '星际战争' },
    { key: 'heikeji', label: '黑科技系统' },
  ],
  mystery: [
    { key: 'zhentan', label: '侦探推理' },
    { key: 'guilyi', label: '诡异神秘' },
    { key: 'guize', label: '规则怪谈' },
    { key: 'lingyi_ms', label: '灵异民俗' },
    { key: 'fengshui', label: '风水秘术' },
    { key: 'xingzhen', label: '刑侦破案' },
    { key: 'daoshu', label: '道术流' },
    { key: 'kesulu', label: '克苏鲁' },
    { key: 'xunyi', label: '悬疑探险' },
    { key: 'xisikongjv', label: '细思极恐' },
    { key: 'shourong', label: '灵异收容' },
  ],
  infinite: [
    { key: 'wuxian', label: '无限流' },
    { key: 'zhutian', label: '诸天流' },
    { key: 'zongman', label: '综漫' },
    { key: 'yuanzu', label: '元祖无限流' },
    { key: 'zhushen', label: '主神流' },
    { key: 'kuaichuan_zt', label: '快穿诸天' },
    { key: 'yingshi_ct', label: '影视世界穿越' },
    { key: 'dongman_ct', label: '动漫世界穿越' },
    { key: 'fuben', label: '副本闯关' },
  ],
  light_novel: [
    { key: 'yuansheng', label: '原生幻想' },
    { key: 'yansheng', label: '衍生同人' },
    { key: 'gaoxiao', label: '搞笑吐槽' },
    { key: 'liana', label: '恋爱日常' },
    { key: 'erciyuan', label: '二次元' },
    { key: 'rixi', label: '日系轻改' },
    { key: 'zhonger', label: '中二设定' },
    { key: 'shacao', label: '沙雕轻松' },
    { key: 'mengxi', label: '萌系' },
    { key: 'yishijie', label: '异世界' },
  ],
  // 女频 ——
  romance: [
    { key: 'dushi_tc', label: '都市甜宠' },
    { key: 'haozong', label: '豪门总裁' },
    { key: 'xianhun', label: '先婚后爱' },
    { key: 'pojing', label: '破镜重圆' },
    { key: 'zhuqi', label: '追妻火葬场' },
    { key: 'bazong', label: '总裁霸总' },
    { key: 'yulequan', label: '娱乐圈' },
    { key: 'zhichang_hl', label: '职场婚恋' },
    { key: 'niandai', label: '年代文' },
    { key: 'xianhun_yw', label: '闪婚' },
    { key: 'xiaoyuan_qc', label: '校园青春' },
    { key: 'nuelian', label: '虐恋情深' },
    { key: 'chongsheng_nx', label: '重生逆袭' },
    { key: 'kuaichuan_yq', label: '快穿' },
  ],
  ancient_romance: [
    { key: 'gongdou', label: '宫斗' },
    { key: 'zhaidou', label: '宅斗' },
    { key: 'gufeng', label: '古风世情' },
    { key: 'gudai_ct', label: '古代穿越' },
    { key: 'shunv', label: '庶女逆袭' },
    { key: 'dinu', label: '嫡女' },
    { key: 'quanchen', label: '权臣' },
    { key: 'jiangjun', label: '将军' },
    { key: 'wangye', label: '王爷' },
    { key: 'daihou', label: '帝后' },
    { key: 'zhongtian_gy', label: '种田经商' },
    { key: 'chaoztang', label: '朝堂权谋' },
    { key: 'daijia', label: '代嫁代娶' },
    { key: 'chongsheng_gy', label: '穿越重生' },
  ],
  fantasy_romance: [
    { key: 'xuanhuan_yq', label: '玄幻言情' },
    { key: 'qihuan_yq', label: '奇幻言情' },
    { key: 'xianxia_yq', label: '仙侠言情' },
    { key: 'xiuxian_yq', label: '修仙言情' },
    { key: 'xuanxue', label: '玄学相师' },
    { key: 'lingyi_yq', label: '灵异言情' },
    { key: 'yineng_nv', label: '异能女主' },
    { key: 'xitong_yq', label: '系统言情' },
    { key: 'chuanshu', label: '穿书' },
    { key: 'weilai_yq', label: '未来言情' },
  ],
  danmei: [
    { key: 'xiandai_ca', label: '现代都市纯爱' },
    { key: 'gudai_ca', label: '古代纯爱' },
    { key: 'xiangxiang_ca', label: '现代幻想纯爱' },
    { key: 'ab0', label: 'ABO' },
    { key: 'qiangqiang', label: '强强' },
    { key: 'tianwen_ca', label: '甜文' },
    { key: 'nuewen_ca', label: '虐文' },
    { key: 'xiaoyuan_ca', label: '校园' },
    { key: 'dianjing_ca', label: '电竞' },
    { key: 'xianxia_ca', label: '仙侠纯爱' },
    { key: 'wuxian_ca', label: '无限流纯爱' },
    { key: 'kuaichuan_ca', label: '快穿纯爱' },
  ],
  acg: [
    { key: 'dongfang_ys', label: '东方衍生' },
    { key: 'xifang_ys', label: '西方衍生' },
    { key: 'gudian_ys', label: '古典衍生' },
    { key: 'erciyuan_yq', label: '二次元言情' },
    { key: 'zongying', label: '综英美' },
    { key: 'zongwuxia', label: '综武侠' },
    { key: 'zongman_ys', label: '综漫' },
    { key: 'yingshi_tr', label: '影视同人' },
    { key: 'youxi_tr', label: '游戏同人' },
    { key: 'dongman_tr', label: '动漫同人' },
  ],
  other: [
    { key: 'xiangsheng', label: '相声评书' },
    { key: 'sanwen', label: '散文随笔' },
    { key: 'pinglun', label: '评论文集' },
    { key: 'youji', label: '美文游记' },
    { key: 'shige', label: '诗歌' },
    { key: 'weixiaoshuo', label: '微小说' },
  ],
};

// ============================================================
// 第三部分：题材 → 风格流派映射（短篇，以知乎盐选/盐言故事赛道为主）
// ============================================================

const SHORT_GENRE_STYLES: Record<string, StyleItem[]> = {
  // 现实情感 / 世情故事（最主流短篇赛道）
  romance: [
    { key: 'hunyin', label: '婚姻信任崩塌' },
    { key: 'poxi', label: '婆媳边界' },
    { key: 'zhichang_pu', label: '职场PUA反杀' },
    { key: 'yuansheng', label: '原生家庭拉扯' },
    { key: 'zhongnian', label: '中年离婚重启' },
    { key: 'chongnianv', label: '重男轻女' },
    { key: 'jiating', label: '家庭伦理' },
    { key: 'chongsheng_dy', label: '重生打脸爽文' },
  ],
  // 古言短篇
  ancient_romance: [
    { key: 'chongsheng_fc', label: '重生复仇' },
    { key: 'shunv_nx', label: '庶女逆袭' },
    { key: 'dinu_fp', label: '嫡姐反派' },
    { key: 'qianshi', label: '前世惨死今生逆袭' },
    { key: 'yinren', label: '隐忍蛰伏摊牌打脸' },
    { key: 'qihun', label: '弃婚另嫁' },
    { key: 'gongdou_dp', label: '宫斗短篇' },
    { key: 'daijia_dp', label: '代嫁代娶' },
    { key: 'xianhun_dp', label: '先婚后爱' },
  ],
  // 现言短篇
  fantasy_romance: [
    { key: 'bazong_zq', label: '霸总追妻火葬场' },
    { key: 'baiyueguang', label: '白月光替身' },
    { key: 'jingshen', label: '净身出户远走' },
    { key: 'chongfeng', label: '重逢反差' },
    { key: 'lihun', label: '离婚后惊艳世界' },
    { key: 'shanhun', label: '闪婚大佬' },
    { key: 'zongcai_bw', label: '总裁卑微求和' },
    { key: 'zhichang_tc', label: '职场甜宠' },
    { key: 'xiaoyuan_cl', label: '校园初恋' },
  ],
  // 悬疑短篇
  mystery: [
    { key: 'shenghuohua', label: '生活化悬疑' },
    { key: 'xisikongju', label: '细思极恐' },
    { key: 'shikong_laidian', label: '时空来电' },
    { key: 'wanmei', label: '完美犯罪' },
    { key: 'duochong_fz', label: '多重反转' },
    { key: 'xiongsha', label: '凶杀推理' },
    { key: 'mishi', label: '密室' },
    { key: 'guize_dp', label: '规则怪谈短篇' },
    { key: 'minshu_dp', label: '民俗志怪' },
    { key: 'jiankong', label: '监控悬疑' },
  ],
  // 灵异短篇
  urban_fantasy: [
    { key: 'lingchen', label: '凌晨怪事' },
    { key: 'jiuwu', label: '旧物惊悚' },
    { key: 'chuzu', label: '出租屋灵异' },
    { key: 'laoxiaoqu', label: '老小区鬼事' },
    { key: 'fengshui_dp', label: '风水秘术' },
    { key: 'daoshi', label: '道士收妖' },
    { key: 'hongyi', label: '红衣女人' },
    { key: 'mintan', label: '民间怪谈' },
  ],
  // 脑洞短篇
  light_novel: [
    { key: 'qingshed', label: '轻设定重映射' },
    { key: 'huangyan', label: '谎言可视化' },
    { key: 'danmu', label: '弹幕生存' },
    { key: 'chaoshi', label: '超现实外壳' },
    { key: 'she_ding', label: '设定撑满五千字' },
    { key: 'qiguai', label: '奇怪群聊' },
    { key: 'zouma', label: '死亡走马灯' },
    { key: 'gonglve', label: '攻略者大逃杀' },
  ],
  // 治愈/情感短篇
  other: [
    { key: 'qingganzhiyu', label: '情感治愈' },
    { key: 'renchong', label: '人宠双向疗愈' },
    { key: 'xishui', label: '细水流长' },
    { key: 'yinanping', label: '意难平' },
    { key: 'houjin', label: '后劲极强' },
    { key: 'wenqing', label: '温情日常' },
    { key: 'shihuai', label: '释怀告别' },
    { key: 'chongfeng_jr', label: '重逢救赎' },
    { key: 'jiashu', label: '家书来信' },
    { key: 'xiaorenwu', label: '小人物温度' },
  ],
  // 爽文短篇（通用兜底，多个题材共用）
  fantasy: [
    { key: 'kaiju', label: '开局一个惨' },
    { key: 'dalian', label: '打脸爽文' },
    { key: 'baofu', label: '净身出户后暴富' },
    { key: 'zhenlong', label: '真龙出狱' },
    { key: 'shenyi_xl', label: '神医下山' },
    { key: 'jianbao_xl', label: '鉴宝赌石' },
    { key: 'shenhao_xt', label: '神豪系统' },
    { key: 'chongsheng_zq', label: '重生赚钱' },
  ],
  // 末世/生存短篇
  scifi: [
    { key: 'moshi_dunhuo', label: '末世囤货' },
    { key: 'anquanwu', label: '安全屋求生' },
    { key: 'gouzh', label: '苟住生存' },
    { key: 'haidao_qz', label: '海岛求生' },
    { key: 'sangshi', label: '丧尸围城' },
    { key: 'yidong', label: '移动基地' },
    { key: 'feitu_dp', label: '废土短篇' },
    { key: 'chongsheng_ms', label: '末世重生' },
  ],
  // 影视化短篇
  infinite: [
    { key: 'qiangqingxu', label: '强情绪快节奏' },
    { key: 'gao_dairu', label: '高代入感' },
    { key: 'suipian', label: '碎片阅读适配' },
    { key: 'gouzi_my', label: '钩子密集' },
    { key: 'duanju', label: '短剧改编向' },
    { key: 'changju', label: '长剧孵化向' },
    { key: 'dy_rencheng', label: '第一人称代入' },
  ],
  // 单元剧/系列短篇
  wuxia: [
    { key: 'danyuan_an', label: '单元案件' },
    { key: 'xilie_zhj', label: '系列主角' },
    { key: 'tanandanyuan', label: '探案单元' },
    { key: 'guaitan_xl', label: '怪谈系列' },
    { key: 'anjian_chuan', label: '案件串烧' },
    { key: 'duanpian_ll', label: '短篇连缀成长篇' },
  ],
  // 真实故事改编
  historical: [
    { key: 'zhenan', label: '真实案件改编' },
    { key: 'guaimai', label: '拐卖案' },
    { key: 'qianwen', label: '奇闻轶事' },
    { key: 'rensheng', label: '人生经历' },
    { key: 'lieqi', label: '猎奇奇案' },
    { key: 'anjiadz_fz', label: '案件反转' },
  ],
  // 大女主短篇
  xianxia: [
    { key: 'chongsheng_nx_dp', label: '重生逆袭' },
    { key: 'bubu', label: '步步为营' },
    { key: 'luanyi', label: '乱世成长' },
    { key: 'chaotang_qm', label: '朝堂权谋' },
    { key: 'guifei', label: '贵妃青云直上' },
    { key: 'shunv_fs', label: '庶女翻身' },
    { key: 'jingcheng', label: '京城第一美人' },
  ],
};

// 短篇"通用赛道兜底"：当题材在 SHORT_GENRE_STYLES 找不到时使用
const SHORT_FALLBACK_STYLES: StyleItem[] = [
  { key: 'fanzhuan', label: '反转向（结局反转）' },
  { key: 'danyuanju', label: '单元剧' },
  { key: 'yingshi', label: '影视化（镜头语言）' },
  { key: 'first_person', label: '第一人称' },
  { key: 'shacao_dp', label: '沙雕爽文' },
  { key: 'dianwen', label: '颠文' },
  { key: 'bailan', label: '摆烂流' },
];

// ============================================================
// 第四部分：对外工具函数
// ============================================================

/**
 * 根据类型 + 题材获取可选风格流派列表。
 * - 长篇：按题材精确匹配，找不到回退到通用流派兜底
 * - 短篇：按题材精确匹配，找不到回退到短篇通用赛道兜底
 * 返回 Record<key, label>，便于前端直接渲染。
 */
export function getStylesForGenre(bookType: string, genre: string): Record<string, string> {
  const table = bookType === 'short_story' ? SHORT_GENRE_STYLES : NOVEL_GENRE_STYLES;
  const items = (genre && table[genre]) || (bookType === 'short_story' ? SHORT_FALLBACK_STYLES : NOVEL_GENRE_STYLES.fantasy);
  const out: Record<string, string> = {};
  for (const it of items) out[it.key] = it.label;
  return out;
}

/**
 * 兼容旧接口：仅按 bookType 返回风格列表（不区分题材，取首个题材兜底）。
 * 新代码请用 getStylesForType(bookType, genre) 做题材联动。
 */
export function getStylesForType(bookType: string): Record<string, string> {
  return getStylesForGenre(bookType, '');
}

/** 根据类型获取卷数范围 */
export function getVolumeRange(bookType: string): { min: number; max: number; default: number; perVolumeWords: string } {
  if (bookType === 'short_story') {
    return { min: 1, max: 3, default: 1, perVolumeWords: '每篇约3-5万字' };
  }
  return { min: 5, max: 30, default: 10, perVolumeWords: '每卷约12万字（约50章）' };
}

/**
 * 题材切换时，过滤掉新题材不支持的已选风格。
 * 用于表单联动：切题材后清掉不合法的已选项。
 */
export function filterStylesByGenre(bookType: string, genre: string, selected: string[]): string[] {
  const available = getStylesForGenre(bookType, genre);
  return selected.filter(k => Object.prototype.hasOwnProperty.call(available, k));
}

// ============================================================
// 第五部分：章节正文「语言风格」（行文文风，区别于题材流派）
// ============================================================
// 用于章节正文 AI 创作时指导本章行文风格，最多可叠加 3 个。
// 数据综合知乎/简书/豆瓣/中国作家网等关于"小说语言风格"的调研整理。
// label 为风格名，desc 为系统说明（注入 AI 提示词指导行文）。

export interface ChapterLangStyle { label: string; desc: string; }

export const CHAPTER_LANG_STYLES: Record<string, ChapterLangStyle> = {
  general: { label: '通用', desc: '行文规范流畅，叙述与对话比例均衡，节奏舒张有度，不刻意炫技也不寡淡；用词准确，符合现代汉语习惯。适合大多数题材的常规叙事。' },
  baimiao: { label: '白描', desc: '用最简练的笔墨勾勒人物与场景，不加渲染烘托；少用形容词，多用动词和名词；叙述客观克制，让事实自己说话。适合动作戏、硬汉派、克制情感。' },
  jijian: { label: '极简', desc: '句子短促有力，信息密度高，大量留白；砍掉一切冗余修饰与过渡；对话简洁，动作直接。适合快节奏、冷硬叙事与悬疑短篇。' },
  youmo: { label: '幽默', desc: '善用夸张、反语、双关与俏皮话制造笑点，插科打诨中藏锋芒；语言口语化，节奏跳跃；笑而不俗、讽而不戾。适合轻松日常、吐槽向、反套路喜剧。' },
  shuangwen: { label: '爽文', desc: '节奏明快，爽点密集，三章一冲突五章一反转；主角步步升级、打脸逆袭；情绪外放，多用反差对比烘托主角强大。适合玄幻都市升级流。' },
  rexue: { label: '热血', desc: '语言激昂奔放，多用短句排比与感叹，动作大开大合；情感外放，强调兄弟情、信念与战斗意志；场面燃点高，节奏层层推进。适合少年向、战斗竞技。' },
  beiqing: { label: '悲情', desc: '语调低沉绵长，多用环境烘托与意象铺陈情感；以克制写伤痛、以细节写离别；不滥情却字字戳心，留有余韵。适合虐心、悲剧、历史向与救赎类。' },
  zhiyu: { label: '治愈', desc: '笔调温柔舒缓，多写日常细节与微小温暖；语言清新柔和，少冲突多陪伴；以烟火气抚慰人心，情绪平稳上扬。适合日常向、慢生活与情感救赎类。' },
  shijing: { label: '市井', desc: '语言俚俗鲜活，多方言口语与江湖切口；人物三教九流，场景茶馆酒肆；叙述带烟火气与油滑感，对白占比较高。适合武侠江湖、都市底层与市井志怪。' },
  gufeng: { label: '古风', desc: '用词典雅，化用诗词典故与文言句式；句式工整讲究韵律，意境含蓄深远；适度文白相间，避免晦涩。适合仙侠、古言、宫斗与历史权谋题材。' },
  guijue: { label: '诡谲', desc: '氛围阴郁压抑，多用阴影、雾气、异响等意象制造不安；叙事扑朔迷离，留悬念与歧义；节奏沉滞中暗藏惊悚。适合悬疑、克苏鲁、志怪灵异与惊悚题材。' },
  shiyi: { label: '诗意', desc: '语言富于音乐性与意象美，重意境与情绪渲染；节奏舒缓，比喻空灵；近似散文诗，以景抒情、以物写心。适合文艺向、情感流与风景心境段落。' },
  kouyu: { label: '口语化', desc: '语言贴近日常口语，句式短、用词俗；可省主语、语序倒置、语气词丰富；叙述如说话，代入感强。适合都市生活、青春校园、第一人称与轻松吐槽向。' },
  huangdan: { label: '荒诞', desc: '以反逻辑与错位制造荒诞感，正经写荒唐、冷静写癫狂；语言可冷面幽默或黑色幽默；解构套路，预期违背生笑点。适合黑色幽默、讽刺、反套路与癫系创作。' },
};

/**
 * 根据已选语言风格 key 列表，拼装注入 AI 的「本章语言风格」指导文本。
 * 返回空串表示未选择（AI 按默认通用风格行文）。
 */
export function buildChapterLangStylePrompt(styleKeys: string[]): string {
  if (!styleKeys || styleKeys.length === 0) return '';
  const parts: string[] = [];
  for (const k of styleKeys.slice(0, 3)) {
    const s = CHAPTER_LANG_STYLES[k];
    if (s) parts.push(`- ${s.label}：${s.desc}`);
  }
  if (parts.length === 0) return '';
  return `【本章语言风格·行文指导】请按以下风格基调行文（可融合）：\n${parts.join('\n')}`;
}
