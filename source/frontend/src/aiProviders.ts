/** 国产 AI 提供商预设（均兼容 OpenAI 接口格式）。
 *
 * 独立成模块的原因：ChatPanel（全局悬浮智驾）与 MinePage（我的/AI配置页）都要用，
 * 若放在 MinePage 里会被 ChatPanel 反向 import，破坏路由级代码分割——ChatPanel 是
 * 全局挂载、启动即加载，会导致 MinePage 代码被提前打进首屏 chunk。
 */

export interface AIProvider {
  value: string;
  label: string;
  icon: string;
  base_url: string;
  model: string;
}

export const AI_PROVIDERS: AIProvider[] = [
  { value: 'deepseek', label: 'DeepSeek 深度求索', icon: '🔵', base_url: 'https://api.deepseek.com/v1', model: 'deepseek-chat' },
  { value: 'qwen', label: '通义千问 阿里', icon: '🟠', base_url: 'https://dashscope.aliyuncs.com/compatible-mode/v1', model: 'qwen-plus' },
  { value: 'glm', label: '智谱GLM', icon: '🟢', base_url: 'https://open.bigmodel.cn/api/paas/v4', model: 'glm-4-flash' },
  { value: 'kimi', label: 'Kimi 月之暗面', icon: '🌙', base_url: 'https://api.moonshot.cn/v1', model: 'moonshot-v1-8k' },
  { value: 'ernie', label: '文心一言 百度', icon: '🔴', base_url: 'https://qianfan.baidubce.com/v2', model: 'ernie-4.0-8k-latest' },
  { value: 'spark', label: '讯飞星火', icon: '⭐', base_url: 'https://spark-api-open.xf-yun.com/v1', model: 'generalv3.5' },
  { value: 'yi', label: '零一万物', icon: '🟣', base_url: 'https://api.lingyiwanwu.com/v1', model: 'yi-large' },
  { value: 'minimax', label: 'MiniMax', icon: '⚫', base_url: 'https://api.minimax.chat/v1', model: 'abab6.5s-chat' },
  { value: 'hunyuan', label: '腾讯混元', icon: '🔷', base_url: 'https://api.hunyuan.cloud.tencent.com/v1', model: 'hunyuan-pro' },
  { value: 'openai', label: 'OpenAI', icon: '🤖', base_url: 'https://api.openai.com/v1', model: 'gpt-4o-mini' },
  { value: 'opencode', label: 'OpenCode Zen 免费', icon: '⚡', base_url: 'https://opencode.ai/zen/v1', model: 'deepseek-v4-flash-free' },
];