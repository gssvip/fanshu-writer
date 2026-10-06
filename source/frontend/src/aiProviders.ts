/** 国产 AI 提供商预设（均兼容 OpenAI 接口格式）。
 *
 * 独立成模块的原因：ChatPanel（全局悬浮智驾）与 MinePage（我的/AI配置页）都要用，
 * 若放在 MinePage 里会被 ChatPanel 反向 import，破坏路由级代码分割——ChatPanel 是
 * 全局挂载、启动即加载，会导致 MinePage 代码被提前打进首屏 chunk。
 */

export interface AIProvider {
  value: string;
  label: string;
  icon: string;       // IconName 语义名称
  color?: string;     // 可选：图标颜色（用于圆形标识）
  base_url: string;
  model: string;
}

export const AI_PROVIDERS: AIProvider[] = [
  { value: 'deepseek', label: 'DeepSeek 深度求索', icon: 'circle', color: '#3b82f6', base_url: 'https://api.deepseek.com/v1', model: 'deepseek-chat' },
  { value: 'qwen', label: '通义千问 阿里', icon: 'circle', color: '#f97316', base_url: 'https://dashscope.aliyuncs.com/compatible-mode/v1', model: 'qwen-plus' },
  { value: 'glm', label: '智谱GLM', icon: 'circle', color: '#22c55e', base_url: 'https://open.bigmodel.cn/api/paas/v4', model: 'glm-4-flash' },
  { value: 'kimi', label: 'Kimi 月之暗面', icon: 'moon', color: '#6366f1', base_url: 'https://api.moonshot.cn/v1', model: 'moonshot-v1-8k' },
  { value: 'ernie', label: '文心一言 百度', icon: 'circle', color: '#ef4444', base_url: 'https://qianfan.baidubce.com/v2', model: 'ernie-4.0-8k-latest' },
  { value: 'spark', label: '讯飞星火', icon: 'star', color: '#eab308', base_url: 'https://spark-api-open.xf-yun.com/v1', model: 'generalv3.5' },
  { value: 'yi', label: '零一万物', icon: 'circle', color: '#a855f7', base_url: 'https://api.lingyiwanwu.com/v1', model: 'yi-large' },
  { value: 'minimax', label: 'MiniMax', icon: 'circle', color: '#6b7280', base_url: 'https://api.minimax.chat/v1', model: 'abab6.5s-chat' },
  { value: 'hunyuan', label: '腾讯混元', icon: 'circle', color: '#0ea5e9', base_url: 'https://api.hunyuan.cloud.tencent.com/v1', model: 'hunyuan-pro' },
  { value: 'openai', label: 'OpenAI', icon: 'bot', color: '#10b981', base_url: 'https://api.openai.com/v1', model: 'gpt-4o-mini' },
  { value: 'opencode', label: 'OpenCode Zen 免费', icon: 'zap', color: '#f59e0b', base_url: 'https://opencode.ai/zen/v1', model: 'deepseek-v4-flash-free' },
  { value: 'openrouter-free', label: 'OpenRouter 免费', icon: 'party', color: '#ec4899', base_url: 'https://openrouter.ai/api/v1', model: 'deepseek/deepseek-chat-v3-0324:free' },
  // Kilo AI Gateway（https://kilo.ai）：统一 API，无需 Key，模型由用户在配置页拉取后自由勾选使用哪些
  { value: 'kilo', label: 'Kilo 免费', icon: 'zap', color: '#0ea5e9', base_url: 'https://api.kilo.ai/api/gateway', model: 'kilo-auto/free' },
];