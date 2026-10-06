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
  // Kilo AI Gateway（https://kilo.ai）：统一 API，仅收录官方 isFree=true 的模型；注册/匿名用户均可拉起。
  { value: 'kilo-free', label: 'Kilo 免费 · 自动选优', icon: 'zap', color: '#0ea5e9', base_url: 'https://api.kilo.ai/api/gateway', model: 'kilo-auto/free' },
  { value: 'kilo-free-llm', label: 'Kilo 免费 · Ling 3.1 Flash', icon: 'zap', color: '#0ea5e9', base_url: 'https://api.kilo.ai/api/gateway', model: 'inclusionai/ling-3.1-flash' },
  { value: 'kilo-free-step', label: 'Kilo 免费 · Step 3.7 Flash', icon: 'zap', color: '#0ea5e9', base_url: 'https://api.kilo.ai/api/gateway', model: 'stepfun/step-3.7-flash:free' },
  { value: 'kilo-free-nemotron-ultra', label: 'Kilo 免费 · Nemotron 3 Ultra', icon: 'zap', color: '#0ea5e9', base_url: 'https://api.kilo.ai/api/gateway', model: 'nvidia/nemotron-3-ultra-550b-a55b:free' },
  { value: 'kilo-free-dots', label: 'Kilo 免费 · Dots3 Note Preview', icon: 'zap', color: '#0ea5e9', base_url: 'https://api.kilo.ai/api/gateway', model: 'dots-studio/dots-3-note-preview:free' },
  { value: 'kilo-free-poolside', label: 'Kilo 免费 · Poolside Laguna S 2.1', icon: 'zap', color: '#0ea5e9', base_url: 'https://api.kilo.ai/api/gateway', model: 'poolside/laguna-s-2.1:free' },
  { value: 'kilo-free-apodex', label: 'Kilo 免费 · Apodex 1.1 Mini', icon: 'zap', color: '#0ea5e9', base_url: 'https://api.kilo.ai/api/gateway', model: 'apodex/apodex-1.1-mini:free' },
  { value: 'kilo-free-ling-sante', label: 'Kilo 免费 · Ling 3.0 Flash Sante', icon: 'zap', color: '#0ea5e9', base_url: 'https://api.kilo.ai/api/gateway', model: 'inclusionai/ling-3.0-flash-sante:free' },
  { value: 'kilo-free-lfm', label: 'Kilo 免费 · LFM2.5-2.6B', icon: 'zap', color: '#0ea5e9', base_url: 'https://api.kilo.ai/api/gateway', model: 'liquid/lfm-2.5-2.6b:free' },
  { value: 'kilo-free-nemotron-lightning', label: 'Kilo 免费 · Nemotron 3.5 Lightning', icon: 'zap', color: '#0ea5e9', base_url: 'https://api.kilo.ai/api/gateway', model: 'nvidia/nemotron-3.5-lightning:free' },
  { value: 'kilo-free-inkling', label: 'Kilo 免费 · Inkling Small', icon: 'zap', color: '#0ea5e9', base_url: 'https://api.kilo.ai/api/gateway', model: 'thinkingmachines/inkling-small:free' },
  { value: 'kilo-free-poolside-xs', label: 'Kilo 免费 · Poolside Laguna XS 2.1', icon: 'zap', color: '#0ea5e9', base_url: 'https://api.kilo.ai/api/gateway', model: 'poolside/laguna-xs-2.1:free' },
  { value: 'kilo-free-cohere', label: 'Kilo 免费 · Cohere North Mini Code', icon: 'zap', color: '#0ea5e9', base_url: 'https://api.kilo.ai/api/gateway', model: 'cohere/north-mini-code:free' },
  { value: 'kilo-free-nemotron-safety', label: 'Kilo 免费 · Nemotron 3.5 Content Safety', icon: 'zap', color: '#0ea5e9', base_url: 'https://api.kilo.ai/api/gateway', model: 'nvidia/nemotron-3.5-content-safety:free' },
  { value: 'kilo-free-nemotron-nano', label: 'Kilo 免费 · Nemotron 3 Nano Omni', icon: 'zap', color: '#0ea5e9', base_url: 'https://api.kilo.ai/api/gateway', model: 'nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free' },
  { value: 'kilo-free-nemotron-super', label: 'Kilo 免费 · Nemotron 3 Super', icon: 'zap', color: '#0ea5e9', base_url: 'https://api.kilo.ai/api/gateway', model: 'nvidia/nemotron-3-super-120b-a12b:free' },
  { value: 'kilo-free-openrouter', label: 'Kilo 免费 · openrouter/free', icon: 'zap', color: '#0ea5e9', base_url: 'https://api.kilo.ai/api/gateway', model: 'openrouter/free' },
];