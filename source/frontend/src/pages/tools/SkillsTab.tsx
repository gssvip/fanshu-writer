import { useState, useEffect, useContext, useRef } from 'react';
import * as yaml from 'js-yaml';
import { api } from '../../api';
import { AuthContext } from '../../App';
import type { SkillPack, WorkflowStep } from '../../types';
import { GENRES, GENRE_GROUPS, normalizeGenreKey } from '../../constants';
import Icon, { type IconName } from '../../components/Icon';

interface SkillEditorState {
  id: string | null;
  name: string;
  icon: string;
  genre: string;
  book_type: string;
  description: string;
  workflow: WorkflowStep[];
  prompts: Record<string, string>;
  // 【三类无污染】技能包分类：master=构思类 / style=文风类 / review=审查类
  category: 'master' | 'style' | 'review';
  genre_target?: string;  // 文风类专属题材标签
}

// 技能包图标选择：使用 IconName 语义名称（12个：桌面端一排、手机端两排各6个）
const ICON_CHOICES: string[] = [
  'package', 'edit-pencil', 'target', 'rocket', 'lightbulb', 'crystal',
  'sword', 'castle', 'heart', 'search', 'bar-chart', 'drama',
];

// 旧 emoji → 新 IconName 映射（向后兼容已有数据）
const EMOJI_TO_ICON: Record<string, IconName> = {
  '📦': 'package', '✍️': 'edit-pencil', '🎯': 'target', '🚀': 'rocket',
  '💡': 'lightbulb', '🔮': 'crystal', '⚔️': 'sword', '🏰': 'castle',
  '❤️': 'heart', '🔍': 'search', '📊': 'bar-chart', '🎭': 'drama',
  '📚': 'book', '🎨': 'palette', '⚡': 'zap',
};

function resolveIcon(icon: string): IconName {
  if (!icon) return 'package';
  if (EMOJI_TO_ICON[icon]) return EMOJI_TO_ICON[icon];
  // 已经是 icon name
  return icon as IconName;
}

function emptySkillEditor(): SkillEditorState {
  return {
    id: null,
    name: '',
    icon: 'package',
    genre: 'other',
    book_type: 'novel',
    description: '',
    workflow: [{ step: 1, name: '', desc: '', prompt_key: '' }],
    prompts: {},
    category: 'master',  // 默认构思类
    genre_target: '',
  };
}

export default function SkillsTab({ selectedBookId }: { selectedBookId: string }) {
  const { requireAuth } = useContext(AuthContext);

  const [loading, setLoading] = useState(false);

  const [skillPacks, setSkillPacks] = useState<SkillPack[]>([]);
  const [skillGenreFilter, setSkillGenreFilter] = useState('');
  const [skillTypeFilter, setSkillTypeFilter] = useState('');
  const [selectedPack, setSelectedPack] = useState<SkillPack | null>(null);
  // 【需求1-1：双击预览】技能包预览Modal状态
  const [previewPack, setPreviewPack] = useState<SkillPack | null>(null);
  // 【三类无污染】技能包市场分组折叠状态（默认全展开）
  const [skillGroupCollapsed, setSkillGroupCollapsed] = useState<Record<string, boolean>>({});

  // 自定义技能编辑器
  const [showSkillEditor, setShowSkillEditor] = useState(false);
  const [skillEditor, setSkillEditor] = useState<SkillEditorState>(emptySkillEditor());
  const [skillSaving, setSkillSaving] = useState(false);
  const [skillError, setSkillError] = useState('');

  // 从 GitHub 同步技能包（拉取最新 SKILL.md 更新提示词）
  const [syncingPackId, setSyncingPackId] = useState<string | null>(null);

  const skillImportRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    api.listSkillPacks().then(setSkillPacks).catch(() => {});
  }, []);

  useEffect(() => {
    api.listSkillPacks(skillGenreFilter || undefined, skillTypeFilter || undefined)
      .then(setSkillPacks).catch(() => {});
  }, [skillGenreFilter, skillTypeFilter]);

  async function handleApplySkillPack() {
    if (!selectedBookId || !selectedPack) return;
    const ok = await requireAuth();
    if (!ok) return;
    setLoading(true);
    try {
      await api.applySkillPack(selectedBookId, selectedPack.id);
      alert(`已应用技能包：${selectedPack.name}`);
      setSelectedPack(null);
    } catch (e: any) { alert('应用失败: ' + e.message); }
    setLoading(false);
  }

  // ---- 自定义技能包 CRUD ----
  function reloadSkillPacks() {
    api.listSkillPacks(skillGenreFilter || undefined, skillTypeFilter || undefined)
      .then(setSkillPacks).catch(() => {});
  }

  function openCreateSkill() {
    setSkillEditor(emptySkillEditor());
    setSkillError('');
    setShowSkillEditor(true);
  }

  function openEditSkill(pack: SkillPack) {
    setSkillEditor({
      id: pack.id,
      name: pack.name,
      icon: pack.icon || 'package',
      genre: pack.genre || 'other',
      book_type: pack.book_type || 'novel',
      description: pack.description || '',
      workflow: pack.workflow?.length ? pack.workflow : [{ step: 1, name: '', desc: '', prompt_key: '' }],
      prompts: pack.prompts || {},
      category: (pack.category || 'master') as 'master' | 'style' | 'review',
      genre_target: pack.genre_target || '',
    });
    setSkillError('');
    setShowSkillEditor(true);
  }

  async function handleSaveSkill() {
    if (!skillEditor.name.trim()) {
      setSkillError('请填写技能包名称');
      return;
    }
    const ok = await requireAuth();
    if (!ok) return;
    setSkillSaving(true);
    setSkillError('');
    try {
      const payload = {
        name: skillEditor.name.trim(),
        icon: skillEditor.icon || 'package',
        genre: skillEditor.genre,
        book_type: skillEditor.book_type,
        description: skillEditor.description,
        workflow: skillEditor.workflow.filter(w => w.name.trim()),
        prompts: skillEditor.prompts,
        category: skillEditor.category,  // 【三类无污染】保存分类
        genre_target: skillEditor.category === 'style' ? (skillEditor.genre_target || skillEditor.genre) : '',  // 文风类自动关联题材
      };
      if (skillEditor.id) {
        await api.updateSkillPack(skillEditor.id, payload);
      } else {
        await api.createSkillPack(payload);
      }
      setShowSkillEditor(false);
      reloadSkillPacks();
    } catch (e: any) {
      setSkillError(e.message || '保存失败');
    }
    setSkillSaving(false);
  }

  async function handleDeleteSkill(pack: SkillPack) {
    if (!confirm(`确定删除技能包"${pack.name}"？此操作不可撤销。`)) return;
    const ok = await requireAuth();
    if (!ok) return;
    try {
      await api.deleteSkillPack(pack.id);
      if (selectedPack?.id === pack.id) setSelectedPack(null);
      reloadSkillPacks();
    } catch (e: any) {
      alert('删除失败: ' + e.message);
    }
  }

  async function handleCloneSkill(pack: SkillPack) {
    const ok = await requireAuth();
    if (!ok) return;
    const name = prompt('为你的技能包命名：', `${pack.name}（我的副本）`);
    if (!name?.trim()) return;
    try {
      await api.cloneSkillPack(pack.id, name.trim());
      reloadSkillPacks();
      alert('已保存到我的技能包');
    } catch (e: any) {
      alert('克隆失败: ' + e.message);
    }
  }

  async function handleEditBuiltinSkill(pack: SkillPack) {
    const ok = await requireAuth();
    if (!ok) return;
    // 【需求2：编辑副本=保存才创建副本】
    // 不立即克隆到数据库，直接打开编辑器编辑副本内容（id=null表示新建）
    // 只有用户点击"保存"时，handleSaveSkill才会通过createSkillPack创建用户副本
    // 这样如果用户打开后放弃编辑（关闭不保存），不会产生多余的空副本
    setSkillEditor({
      id: null,
      name: `${pack.name}（我的副本）`,
      icon: pack.icon || 'package',
      genre: pack.genre || 'other',
      book_type: pack.book_type || 'novel',
      description: pack.description || '',
      workflow: pack.workflow?.length ? JSON.parse(JSON.stringify(pack.workflow)) : [{ step: 1, name: '', desc: '', prompt_key: '' }],
      prompts: pack.prompts ? JSON.parse(JSON.stringify(pack.prompts)) : {},
      category: (pack.category || 'master') as 'master' | 'style' | 'review',
      genre_target: pack.genre_target || '',
    });
    setSkillError('');
    setShowSkillEditor(true);
  }

  async function handlePublishSkill(pack: SkillPack) {
    const ok = await requireAuth();
    if (!ok) return;
    if (!confirm(`确定将"${pack.name}"分享到系统技能包？分享后所有用户都能使用。`)) return;
    try {
      await api.publishSkillPack(pack.id);
      reloadSkillPacks();
      alert('已分享到系统技能包，其他用户现在也可以使用了');
    } catch (e: any) {
      alert('分享失败: ' + e.message);
    }
  }

  async function handleSyncFromGitHub(pack: SkillPack) {
    const ok = await requireAuth();
    if (!ok) return;
    if (!pack.github_source) { alert('该技能包未关联 GitHub 仓库'); return; }
    if (!confirm(`从 GitHub 拉取最新版本？\n源：${pack.github_source}\n这会更新技能包中各步骤的提示词。`)) return;
    setSyncingPackId(pack.id);
    try {
      const res = await api.syncSkillPackFromGitHub(pack.id);
      reloadSkillPacks();
      let msg = `✅ ${res.message}`;
      if (res.errors && res.errors.length > 0) msg += `\n\n部分失败：\n${res.errors.join('\n')}`;
      alert(msg);
    } catch (e: any) {
      alert('同步失败: ' + e.message);
    }
    setSyncingPackId(null);
  }

  function addWorkflowStep() {
    setSkillEditor(prev => ({
      ...prev,
      workflow: [...prev.workflow, { step: prev.workflow.length + 1, name: '', desc: '', prompt_key: '' }],
    }));
  }

  function updateWorkflowStep(idx: number, field: keyof WorkflowStep, val: string) {
    setSkillEditor(prev => ({
      ...prev,
      workflow: prev.workflow.map((w, i) => i === idx ? { ...w, [field]: val, step: i + 1 } : w),
    }));
  }

  function updateWorkflowTemp(idx: number, temp: number) {
    setSkillEditor(prev => ({
      ...prev,
      workflow: prev.workflow.map((w, i) => i === idx ? { ...w, temperature: temp } : w),
    }));
  }

  function removeWorkflowStep(idx: number) {
    setSkillEditor(prev => ({
      ...prev,
      workflow: prev.workflow.filter((_, i) => i !== idx).map((w, i) => ({ ...w, step: i + 1 })),
    }));
  }

  function addPrompt() {
    setSkillEditor(prev => {
      const n = Object.keys(prev.prompts).length;
      const key = `prompt_${n + 1}`;
      return { ...prev, prompts: { ...prev.prompts, [key]: '' } };
    });
  }

  function updatePromptKey(oldKey: string, newKey: string) {
    setSkillEditor(prev => {
      const entries = Object.entries(prev.prompts);
      const updated: Record<string, string> = {};
      for (const [k, v] of entries) updated[k === oldKey ? newKey : k] = v;
      return { ...prev, prompts: updated };
    });
  }

  function updatePromptValue(key: string, val: string) {
    setSkillEditor(prev => ({ ...prev, prompts: { ...prev.prompts, [key]: val } }));
  }

  function removePrompt(key: string) {
    setSkillEditor(prev => {
      const { [key]: _, ...rest } = prev.prompts;
      return { ...prev, prompts: rest };
    });
  }

  async function handleExportSkill(pack: SkillPack) {
    const exportData = {
      name: pack.name, icon: pack.icon, genre: pack.genre, book_type: pack.book_type,
      description: pack.description, workflow: pack.workflow, prompts: pack.prompts,
    };
    const blob = new Blob([JSON.stringify(exportData, null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `${pack.name}-技能包.json`;
    a.click();
    URL.revokeObjectURL(url);
  }

  async function handleImportSkill(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    if (!file) return;
    try {
      const rawText = await file.text();
      const data = smartParseSkillPack(rawText, file.name);
      if (!data) {
        alert('无法解析文件内容：请粘贴或导入 ① skill.md(带---分隔的front-matter) ② JSON ③ YAML ④ 任意纯文本（会作为提示词自动转技能包）');
        return;
      }

      const ok = await requireAuth();
      if (!ok) return;
      // 【题材对齐】导入时一定按GENRES真相源归一化genre/genre_target，避免"穿越/修真/甜宠"等别名落库成unknown key
      const genre = normalizeGenreKey(data.genre);
      const genre_target = normalizeGenreKey(data.genre_target);
      const payload = {
        name: data.name,
        icon: data.icon || 'package',
        genre,
        genre_target: genre_target || undefined,
        book_type: data.book_type || 'novel',
        description: data.description || '',
        category: data.category || 'master',
        workflow: Array.isArray(data.workflow) ? data.workflow : [],
        prompts: data.prompts || {},
      };
      await api.createSkillPack(payload);
      alert(`技能包 "${payload.name}" 导入成功`);
      reloadSkillPacks();
    } catch (err: any) {
      alert('导入失败: ' + (err.message || '请检查文件格式'));
    }
    if (skillImportRef.current) skillImportRef.current.value = '';
  }

  /**
   * 多格式智能解析器：
   *  优先级 1) 代码块内的 JSON/YAML（```json / ```yaml）
   *  优先级 2) Markdown front-matter（---分隔块，兼容中文冒号"："）
   *  优先级 3) 整段 JSON
   *  优先级 4) 整段 YAML
   *  优先级 5) 纯文本兜底：自动转 {name, description, category, prompts:{default}}
   */
  function smartParseSkillPack(rawText: string, fileName: string = ''): any {
    if (!rawText || !rawText.trim()) return null;
    const trimmed = rawText.trim();
    let data: any = null;
    let bodyText = '';  // front-matter 之后的正文 / 纯文本兜底的正文，用于工作流步骤识别

    // ① md代码块先扫
    const yamlBlockMatch = trimmed.match(/```(?:yaml|yml)\s*\r?\n([\s\S]*?)\r?\n?```/i);
    if (yamlBlockMatch) {
      try { data = yaml.load(yamlBlockMatch[1].trim()) as any; } catch { /* ignore */ }
    }
    const jsonBlockMatch = !data ? trimmed.match(/```(?:json)?\s*\r?\n([\s\S]*?)\r?\n?```/i) : null;
    if (!data && jsonBlockMatch) {
      try { data = JSON.parse(jsonBlockMatch[1].trim()); } catch {
        try { data = yaml.load(jsonBlockMatch[1].trim()) as any; } catch { /* ignore */ }
      }
    }

    // ② front-matter（兼容用户的中文冒号 "name：xxx" 全角写法，必须在文本最开头）
    if (!data) {
      const fmMatch = trimmed.match(/^---\r?\n([\s\S]*?)\r?\n---(?:\r?\n([\s\S]*))?$/);
      if (fmMatch) {
        const header = fmMatch[1];
        const body = fmMatch[2] || '';
        if (body.trim()) bodyText = body.trim();
        const normalized = header
          // 兼容：key：value (中文全角冒号/两边空格) 统一为 key: value
          .replace(/^([^\s：:][^：:\n]*?)[ \t]*[：:][ \t]*/gm, (_, k) => `${k}: `);
        try {
          data = yaml.load(normalized) as any;
          if (data && typeof data === 'object' && body && body.trim()) {
            // 把front-matter之后的markdown正文作为默认提示词注入
            if (!data.prompts || typeof data.prompts !== 'object') data.prompts = {};
            const defaultKey = data.category === 'style' ? 'style_default'
              : data.category === 'review' ? 'review_default' : 'master_default';
            if (!data.prompts[defaultKey]) data.prompts[defaultKey] = body.trim();
            if (!data.description) {
              // 正文前120字提炼描述
              data.description = body.trim().slice(0, 120).replace(/\s+/g, ' ');
            }
          }
        } catch { /* ignore */ }
      }
    }

    // ③ 整段JSON
    if (!data && (trimmed.startsWith('{') || trimmed.startsWith('['))) {
      try { data = JSON.parse(trimmed); } catch { /* ignore */ }
    }
    // ④ 整段YAML
    if (!data) {
      try { data = yaml.load(trimmed) as any; } catch { /* ignore */ }
    }

    // ⑤ 纯文本兜底：自动推断 name/description/prompts
    if (!data || typeof data !== 'object') {
      const body = (data && typeof data === 'string') ? data : rawText;
      if (!body || !String(body).trim()) return null;
      bodyText = String(body).trim();
      const firstLine = String(body).split(/\r?\n/).map(l => l.trim()).find(l => l.length) || '我的技能';
      const cleanName = firstLine.replace(/^[#\s\-*>`"']+|[#\s\-*>`"']+$/g, '').slice(0, 40) || '自定义技能包';
      const plainDesc = String(body).trim().replace(/\s+/g, ' ').slice(0, 160);
      data = {
        name: cleanName,
        description: plainDesc,
        category: inferCategoryFromText(body),
        prompts: {
          master_default: String(body).trim(),
        },
      };
    }

    // 格式规范：无name → 用fileName首行兜底
    if (!data.name) {
      data.name = fileName ? fileName.replace(/\.[^.]+$/, '') : '自定义技能包';
    }
    // 【题材对齐】最终把genre/genre_target归一化成GENRES真相源的合法key（兜底other），
    // 同时支持用户写中文标签/别名（如"修真"/"甜宠"/"末世"/"规则怪谈"）自动映射
    data.genre = normalizeGenreKey(data.genre);
    if (data.genre_target !== undefined && data.genre_target !== null) {
      data.genre_target = normalizeGenreKey(data.genre_target);
    }
    // 规范化：prompts 兜底
    if (!data.prompts || typeof data.prompts !== 'object') {
      // 如果用户给的是string文本但走到这里，兜底塞进 prompts.default
      const fallback = typeof data.content === 'string' ? data.content
        : typeof data.prompt === 'string' ? data.prompt
        : typeof data.body === 'string' ? data.body : '';
      data.prompts = { default: fallback || String(rawText).trim().slice(0, 4000) };
    }

    // ===== 工作流自动识别（导入文件 / 新建粘贴 两条路径共用）=====
    // ① 先规范化 workflow 本身：字符串步骤、别名字段（title/description/prompt）、内联提示词自动落到 prompts
    data.workflow = normalizeWorkflowSteps(data.workflow, data.prompts);
    // ② workflow 为空 → 从正文里提取步骤（"## 工作流"小节、"### 1. 步骤名"标题、"1. 步骤名"/"步骤1：xxx"编号列表）
    if (!data.workflow.length && bodyText) {
      const extracted = extractWorkflowFromText(bodyText);
      if (extracted && extracted.workflow.length) {
        data.workflow = extracted.workflow;
        data.prompts = { ...extracted.prompts, ...data.prompts };
        // 正文已拆成步骤提示词时，去掉与整段正文重复的兜底提示词，避免重复
        for (const k of Object.keys(data.prompts)) {
          if ((data.prompts[k] || '').trim() === bodyText) delete data.prompts[k];
        }
      }
    }
    // ③ 仍无 workflow 但 prompts 有多个键 → 按键名生成步骤（prompt_key 与键名一一对应）
    if (!data.workflow.length) {
      const stepKeys = Object.keys(data.prompts).filter(k => !['default', 'master_default', 'style_default', 'review_default'].includes(k));
      if (stepKeys.length >= 2) {
        data.workflow = stepKeys.map((k, i) => ({ step: i + 1, name: k, desc: '', prompt_key: k }));
      }
    }
    return data;
  }

  /** 关键词轻量推断：正文/文风/审查 */
  function inferCategoryFromText(text: string): 'master' | 'style' | 'review' {
    const t = String(text);
    const styleHits = (t.match(/句式|文风|用词|口语|节奏|排比|短句|长句|方言|对白|叙述密度|标点|修辞|比喻|形容词|动词/g) || []).length;
    const reviewHits = (t.match(/一致性|去AI|审核|AI味|校验|冲突|矛盾|伏笔|设定检查|设定一致|错别字|病句|逻辑/g) || []).length;
    if (reviewHits >= styleHits && reviewHits >= 2) return 'review';
    if (styleHits >= 2) return 'style';
    return 'master';
  }

  /**
   * 工作流步骤自动识别：从 skill.md / 纯文本正文里提取工作流步骤，支持三种常见写法——
   *   ① 标题式：### 1. 设定构建 / ## 步骤一：设定构建
   *   ② 编号列表：1. 设定构建：搭建世界观  /  2）大纲生成
   *   ③ "步骤N"式：步骤1：设定构建
   * 每个步骤正文自动落到 prompts[prompt_key]（可用 "prompt_key：xxx" / "提示词键名：xxx" 显式指定键名，
   * 缺省为 step_N），并把 name/desc/prompt_key 填进工作流对应位置。
   */
  function extractWorkflowFromText(text: string): { workflow: WorkflowStep[]; prompts: Record<string, string> } | null {
    if (!text || !text.trim()) return null;
    const lines = String(text).split(/\r?\n/);
    // 步骤起点行：可选 markdown 标题/列表符号 + 可选"步骤/第N步"前缀 + 序号 + 分隔符 + 名称
    const stepLineRe = /^(?:#{1,6}\s*)?(?:[-*+]\s+|\s{0,3})(?:\*\*)?(?:步骤\s*|第\s*)?([0-9]{1,2}|[一二三四五六七八九十]{1,3})\s*(?:步)?[.、)）:：]\s*(?:\*\*)?\s*(.+?)(?:\*\*)?\s*$/;
    const headingRe = /^#{1,6}\s+\S/;
    const keyRe = /(?:prompt_key|prompt\s*key|提示词键名|提示词键)\s*[：:]\s*([A-Za-z0-9_\-]+)/i;
    const descRe = /(?:^|\s)(?:desc|description|说明|简介)\s*[：:]\s*(.+)$/i;
    const promptLabelRe = /^(?:\*\*)?\s*(?:提示词|prompt)(?:模板|模板内容|内容)?\s*[：:]?\s*(?:\*\*)?\s*$/i;

    interface Seg { name: string; desc: string; body: string[]; explicit: boolean }
    const segs: Seg[] = [];
    let cur: Seg | null = null;
    for (const rawLine of lines) {
      const m = rawLine.match(stepLineRe);
      if (m && m[2]) {
        let name = m[2].trim();
        let desc = '';
        const nm = name.match(/^([^：:]+)[：:]\s*(.+)$/);
        if (nm) { name = nm[1].trim(); desc = nm[2].trim(); }
        name = name.replace(/\*+/g, '').trim();
        // 明确的步骤标记（markdown标题 / "步骤N" / "第N步"）→ 单步也可识别
        const explicit = /^\s*#/.test(rawLine) || /步骤|第\s*[0-9一二三四五六七八九十]+\s*步|step/i.test(rawLine);
        cur = { name, desc, body: [], explicit };
        segs.push(cur);
        continue;
      }
      // 其他标题（非步骤）→ 截断当前步骤正文，防止把下一节内容吞进提示词
      if (headingRe.test(rawLine)) {
        if (cur) cur = null;
        continue;
      }
      if (cur && !promptLabelRe.test(rawLine)) cur.body.push(rawLine);
    }

    const valid = segs.filter(s => s.name);
    if (!valid.length) return null;
    // 普通编号列表至少 2 步才算工作流（避免把提示词里的"1. 2."清单误判）；带明确步骤标记的 1 步也认
    if (valid.length < 2 && !valid.some(s => s.explicit)) return null;

    const workflow: WorkflowStep[] = [];
    const prompts: Record<string, string> = {};
    valid.forEach((seg, i) => {
      let promptKey = '';
      let desc = seg.desc;
      const contentLines: string[] = [];
      for (const l of seg.body) {
        const km = l.match(keyRe);
        if (km && !promptKey) { promptKey = km[1].trim(); continue; }
        const dm = !desc ? l.match(descRe) : null;
        if (dm) { desc = dm[1].trim(); continue; }
        contentLines.push(l);
      }
      const content = contentLines.join('\n').trim();
      if (!promptKey && content) promptKey = `step_${i + 1}`;
      if (content && promptKey) prompts[promptKey] = content;
      workflow.push({ step: i + 1, name: seg.name, desc, prompt_key: promptKey });
    });
    return { workflow, prompts };
  }

  /**
   * 规范化导入/粘贴得到的 workflow：兼容字符串步骤和别名字段（title/description/prompt/key），
   * 步骤里内联的提示词正文自动落到 prompts[prompt_key]，与工作流"对应位置"关联。
   */
  function normalizeWorkflowSteps(raw: any, prompts: Record<string, string>): WorkflowStep[] {
    if (!Array.isArray(raw)) return [];
    const out: WorkflowStep[] = [];
    for (const item of raw) {
      if (item == null) continue;
      let name = '', desc = '', promptKey = '', inline = '';
      let temp: number | undefined;
      if (typeof item === 'string') {
        // "名称 | 说明 | prompt_key" 或 "1. 名称：说明"
        const parts = item.split('|').map(s => s.trim());
        name = (parts[0] || '')
          .replace(/^(?:步骤\s*|第\s*)?[0-9一二三四五六七八九十]{1,3}\s*(?:步)?[.、)）:：]?\s*/, '')
          .replace(/\*+/g, '').trim();
        const nm = name.match(/^([^：:]+)[：:]\s*(.+)$/);
        if (nm) { name = nm[1].trim(); desc = nm[2].trim(); }
        if (parts[1]) desc = parts[1];
        promptKey = parts[2] || '';
      } else if (typeof item === 'object') {
        name = String(item.name ?? item.title ?? item.step_name ?? item.label ?? '').replace(/\*+/g, '').trim();
        desc = String(item.desc ?? item.description ?? item.summary ?? '').trim();
        const pk = item.prompt_key ?? item.promptKey ?? item.prompt_id ?? item.key;
        if (typeof pk === 'string' && pk.trim()) promptKey = pk.trim();
        const rawPrompt = item.prompt_text ?? item.content ?? item.prompt ?? item.body ?? item.template;
        if (typeof rawPrompt === 'string' && rawPrompt.trim()) {
          // prompt 字段短横线命名 → 当键名；否则当内联提示词正文
          if (!promptKey && /^[A-Za-z0-9_\-]{1,40}$/.test(rawPrompt.trim())) {
            promptKey = rawPrompt.trim();
          } else {
            inline = rawPrompt.trim();
          }
        }
        const t = Number(item.temperature ?? item.temp);
        if (Number.isFinite(t) && t >= 0 && t <= 2) temp = t;
      }
      if (!name && !promptKey && !inline) continue;
      if (!promptKey && inline) promptKey = `step_${out.length + 1}`;
      if (inline && promptKey && !prompts[promptKey]) prompts[promptKey] = inline;
      out.push({ step: out.length + 1, name, desc, prompt_key: promptKey, ...(temp !== undefined ? { temperature: temp } : {}) });
    }
    return out;
  }

  /** 创建/编辑表单里：把用户粘贴的文本识别成字段（保留用户已手动填好的name/category/description不覆盖） */
  function applySmartParseToEditor(rawText: string) {
    const parsed = smartParseSkillPack(rawText, skillEditor.name + '.txt');
    if (!parsed) return;
    setSkillEditor(prev => {
      const next: typeof prev = { ...prev };
      // 不覆盖用户已经手动填的字段（只有空值才从解析结果里补）
      if (!next.name && parsed.name) next.name = parsed.name;
      if (!next.description && parsed.description) next.description = parsed.description;
      if (parsed.category && !next.category) next.category = parsed.category;
      if (parsed.icon && !next.icon) next.icon = parsed.icon;
      if (parsed.genre && !next.genre) next.genre = parsed.genre;
      if (parsed.workflow?.length) {
        // 用户已有步骤为空时覆盖，否则保留
        const userStepsEmpty = !next.workflow?.length || next.workflow.every(w => !w.name && !w.prompt_key);
        if (userStepsEmpty) next.workflow = parsed.workflow;
      }
      if (parsed.prompts && typeof parsed.prompts === 'object' && Object.keys(parsed.prompts).length) {
        next.prompts = { ...(parsed.prompts as Record<string, string>), ...next.prompts };
      }
      return next;
    });
  }

  return (
    <div className="tool-panel">
      <div className="skills-header">
        <div>
          <h3><Icon name="package" size={16} /> 技能包市场</h3>
        </div>
        <div className="skills-header-actions">
          <input ref={skillImportRef} type="file" accept=".json,.md,.yaml,.yml" onChange={handleImportSkill} style={{display:'none'}} id="skill-import-input" />
          <button className="btn-secondary" onClick={() => skillImportRef.current?.click()}><Icon name="inbox" size={14} /> 导入技能</button>
          <button className="btn-primary" onClick={openCreateSkill}>+ 创建自定义</button>
        </div>
      </div>
      <div className="form-row">
        <select className="input" value={skillGenreFilter} onChange={e => setSkillGenreFilter(e.target.value)}>
          <option value="">所有题材</option>
          {GENRE_GROUPS.map(g => (
            <optgroup key={g.label} label={g.label}>
              {g.keys.map(k => <option key={k} value={k}>{GENRES[k] || k}</option>)}
            </optgroup>
          ))}
        </select>
        <select className="input" value={skillTypeFilter} onChange={e => setSkillTypeFilter(e.target.value)}>
          <option value="">所有类型</option>
          <option value="novel">长篇</option>
          <option value="short_story">短篇</option>
        </select>
      </div>
      {/* 【三类无污染】技能包市场按三类分组展示 */}
      {(() => {
        const renderSkillCard = (pack: SkillPack) => (
          <div key={pack.id} className={`skill-card ${selectedPack?.id === pack.id ? 'selected' : ''}`} onClick={() => setSelectedPack(pack)} onDoubleClick={() => setPreviewPack(pack)} title="单击选中 · 双击预览">
            <div className="skill-card-header">
              <span className="skill-card-icon"><Icon name={resolveIcon(pack.icon)} size={24} /></span>
              <div>
                <div className="skill-card-name">{pack.name}{pack.is_builtin ? <span className="builtin-badge">系统</span> : <span className="custom-badge">自定义</span>}</div>
                <div className="skill-card-genre">{GENRES[pack.genre] || pack.genre} · {pack.book_type === 'novel' ? '长篇' : '短篇'}</div>
              </div>
              <div className="skill-card-actions" onClick={e => e.stopPropagation()}>
                {pack.is_builtin ? (
                  <>
                    <button className="btn-icon" title="另存为我的技能" onClick={() => handleCloneSkill(pack)}><Icon name="clipboard" size={16} /></button>
                    <button className="btn-icon" title="编辑副本" onClick={() => handleEditBuiltinSkill(pack)}><Icon name="edit" size={16} /></button>
                    <button className="btn-icon" title="导出" onClick={() => handleExportSkill(pack)}><Icon name="upload" size={16} /></button>
                  </>
                ) : (
                  <>
                    <button className="btn-icon" title="分享到系统" onClick={() => handlePublishSkill(pack)}><Icon name="globe" size={16} /></button>
                    <button className="btn-icon" title="导出" onClick={() => handleExportSkill(pack)}><Icon name="upload" size={16} /></button>
                    <button className="btn-icon" title="编辑" onClick={() => openEditSkill(pack)}><Icon name="edit" size={16} /></button>
                    <button className="btn-icon" title="删除" onClick={() => handleDeleteSkill(pack)}><Icon name="trash" size={16} /></button>
                  </>
                )}
              </div>
            </div>
            <div className="skill-card-desc">{pack.description}</div>
            {pack.github_source && (
              <div style={{ marginTop: 6, padding: '6px 8px', background: 'var(--bg-tertiary)', borderRadius: 6, fontSize: 11, color: 'var(--text-secondary)', display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 8 }}>
                <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                  <Icon name="link" size={12} /> GitHub: {pack.github_synced_at ? `已同步 ${new Date(pack.github_synced_at).toLocaleDateString()}` : '未同步'}
                </span>
                <button
                  className="btn-secondary-sm"
                  style={{ flexShrink: 0 }}
                  disabled={syncingPackId === pack.id}
                  onClick={(e) => { e.stopPropagation(); handleSyncFromGitHub(pack); }}
                  title={`从 ${pack.github_source} 拉取最新版本`}
                >
                  {syncingPackId === pack.id ? <><Icon name="hourglass" size={12} /> 同步中</> : <><Icon name="refresh" size={12} /> 同步GitHub</>}
                </button>
              </div>
            )}
            <div className="skill-card-workflow">
              {pack.workflow?.map((step, i) => (
                <div key={i} className="workflow-step"><span className="workflow-step-num">{step.step}</span><span>{step.name}</span></div>
              ))}
            </div>
          </div>
        );
        const masterPacks = skillPacks.filter(p => (p.category || 'master') === 'master');
        const stylePacks = skillPacks.filter(p => p.category === 'style');
        const reviewPacks = skillPacks.filter(p => p.category === 'review');
        const renderGroup = (title: string, packs: SkillPack[], hint: string, color: string) => packs.length === 0 ? null : (
          <div key={title} className="skill-category-group" style={{ borderLeft: `3px solid ${color}`, paddingLeft: 12, marginBottom: 16 }}>
            <button
              type="button"
              className="skill-category-toggle"
              onClick={() => setSkillGroupCollapsed(prev => ({ ...prev, [title]: !prev[title] }))}
              style={{ background: 'none', border: 'none', cursor: 'pointer', padding: 0, display: 'flex', alignItems: 'center', gap: 6, width: '100%', textAlign: 'left' }}
            >
              <span style={{ fontSize: 12, color: 'var(--text-muted)', transition: 'transform 0.2s', transform: skillGroupCollapsed[title] ? 'rotate(-90deg)' : 'rotate(0deg)' }}>▼</span>
              <h4 style={{ margin: 0, fontSize: 14, color: 'var(--text-primary)', flex: 1 }}>
                {title} <span style={{ fontSize: 11, color: 'var(--text-muted)', fontWeight: 400 }}>({packs.length})</span>
              </h4>
            </button>
            {!skillGroupCollapsed[title] && (
              <>
                <p style={{ fontSize: 11, color: 'var(--text-muted)', margin: '4px 0 8px 18px' }}>{hint}</p>
                <div className="skill-grid" style={{ marginLeft: 18 }}>{packs.map(renderSkillCard)}</div>
              </>
            )}
          </div>
        );
        return (
          <>
            {renderGroup('构思类', masterPacks, '大纲/规划/设定/总创作阶段注入，提供创作方法论', '#4a90d9')}
            {renderGroup('文风类', stylePacks, '正文生成阶段注入，按题材锚定句式/节奏/用词风格', '#d97706')}
            {renderGroup('审查类', reviewPacks, '去AI味/一致性检查阶段注入，规范审校规则', '#059669')}
          </>
        );
      })()}
      {selectedPack && (
        <div className="skill-apply-bar">
          <span>将 "{selectedPack.name}" 应用到当前作品</span>
          <button className="btn-primary" onClick={handleApplySkillPack} disabled={!selectedBookId || loading}>
            {loading ? '应用中...' : '应用技能包'}
          </button>
          <button className="btn-ghost" onClick={() => setSelectedPack(null)}>取消</button>
        </div>
      )}

      {/* 自定义技能编辑器 */}
      {showSkillEditor && (
        <div className="skill-editor-overlay" onClick={() => setShowSkillEditor(false)}>
          <div className="skill-editor-modal" onClick={e => e.stopPropagation()}>
            <div className="skill-editor-header">
              <h3>{skillEditor.id ? '编辑技能包' : '创建自定义技能包'}</h3>
              <button className="btn-icon" onClick={() => setShowSkillEditor(false)}><Icon name="x" size={16} /></button>
            </div>

            {skillError && <div className="error-msg">{skillError}</div>}

            <div className="form-field">
              <label>图标</label>
              <div className="emoji-picker">
                {ICON_CHOICES.map(ic => (
                  <button key={ic} className={`emoji-chip ${skillEditor.icon === ic ? 'active' : ''}`} onClick={() => setSkillEditor(prev => ({ ...prev, icon: ic }))}><Icon name={resolveIcon(ic)} size={18} /></button>
                ))}
              </div>
            </div>

            <div className="form-field">
              <label>名称 *</label>
              <input className="input" value={skillEditor.name} onChange={e => setSkillEditor(prev => ({ ...prev, name: e.target.value }))} placeholder="如：悬疑推理创作流" />
            </div>

            {/* 题材/类型同一排平齐、两列等宽（移动端也保持一行） */}
            <div className="form-row skill-meta-row">
              <div className="form-field">
                <label>题材</label>
                <select className="input" value={skillEditor.genre} onChange={e => setSkillEditor(prev => ({ ...prev, genre: e.target.value }))}>
                  {GENRE_GROUPS.map(g => (
                    <optgroup key={g.label} label={g.label}>
                      {g.keys.map(k => <option key={k} value={k}>{GENRES[k] || k}</option>)}
                    </optgroup>
                  ))}
                </select>
              </div>
              <div className="form-field">
                <label>类型</label>
                <select className="input" value={skillEditor.book_type} onChange={e => setSkillEditor(prev => ({ ...prev, book_type: e.target.value }))}>
                  <option value="novel">长篇</option>
                  <option value="short_story">短篇</option>
                </select>
              </div>
            </div>
            {/* 文风类：额外显示「适用题材 (genre_target)」下拉，和创建小说表单题材条目完全对齐 */}
            {skillEditor.category === 'style' && (
              <div className="form-field">
                <label>文风适用题材（正文阶段按题材优先匹配）</label>
                <select className="input"
                  value={skillEditor.genre_target || ''}
                  onChange={e => setSkillEditor(prev => ({ ...prev, genre_target: e.target.value }))}>
                  <option value="">不指定（任意题材生效）</option>
                  {GENRE_GROUPS.map(g => (
                    <optgroup key={g.label} label={g.label}>
                      {g.keys.map(k => <option key={k} value={k}>{GENRES[k] || k}</option>)}
                    </optgroup>
                  ))}
                </select>
              </div>
            )}

            {/* 【三类无污染】技能包分类选择：决定该包在哪个创作阶段注入 */}
            <div className="form-field">
              <label>技能包分类 *</label>
              <select className="input" value={skillEditor.category} onChange={e => setSkillEditor(prev => ({ ...prev, category: e.target.value as 'master' | 'style' | 'review' }))}>
                <option value="master">构思类（大纲/规划/设定阶段注入）</option>
                <option value="style">文风类（正文生成阶段注入，按题材锚定文风）</option>
                <option value="review">审查类（去AI味/一致性检查阶段注入）</option>
              </select>
            </div>

            <div className="form-field">
              <label>描述</label>
              <textarea className="input" rows={2} value={skillEditor.description} onChange={e => setSkillEditor(prev => ({ ...prev, description: e.target.value }))} placeholder="简要描述这个技能包的用途和适用场景" />
            </div>

            {/* 【用户诉求-易用性】直接粘贴任意文本 / skill.md / JSON/YAML，自动识别成技能包 */}
            <div className="form-field">
              <label style={{display:'inline-flex',alignItems:'center',gap:4}}><Icon name="lightbulb" size={14} /> 粘贴提示词或 skill.md 自动识别</label>
              <textarea
                className="input"
                rows={5}
                placeholder={`示例1（skill.md front-matter）：
---
name：我的悬疑构思法
description：适合悬疑推理题材的大纲创作指南
category：master
---
1. 开篇三秒内抛出一个不可能的谜题...

示例2（纯文本）：
  短句占比 ≥60%，冲突场景每段≤15字。对白提示语放句中/句后比例 ≥70%。比喻拟人每千字≤3处。`}
                onPasteCapture={(e) => {
                  // 粘贴时做一次识别填充（非破坏性：已填的 name/category/description 保留）
                  const text = e.clipboardData?.getData('text');
                  if (!text) return;
                  setTimeout(() => applySmartParseToEditor(text), 0);
                }}
                onBlur={(e) => {
                  // 失焦也做一次识别，覆盖粘贴时漏掉的
                  if (e.target.value && e.target.value.trim().length > 20) {
                    applySmartParseToEditor(e.target.value);
                    e.target.value = '';  // 识别清空，避免下次再跑
                  }
                }}
              />
            </div>

            <div className="form-field">
                <label>创作工作流步骤</label>
                {skillEditor.workflow.map((step, idx) => (
                  <div key={idx} className="workflow-editor-step">
                    <span className="workflow-step-num">{idx + 1}</span>
                    <input className="input wf-name" value={step.name} onChange={e => updateWorkflowStep(idx, 'name', e.target.value)} placeholder="步骤名称（如：设定构建）" />
                    <input className="input wf-desc" value={step.desc} onChange={e => updateWorkflowStep(idx, 'desc', e.target.value)} placeholder="步骤说明" />
                    <input className="input wf-key" value={step.prompt_key} onChange={e => updateWorkflowStep(idx, 'prompt_key', e.target.value)} placeholder="prompt键名" />
                    <label className="wf-temp" title="该步骤采样温度（0~2，缺省跟随全局配置）">
                      <Icon name="thermometer" size={14} /> 温度
                      <input type="number" min={0} max={2} step={0.1}
                        className="input"
                        value={step.temperature ?? ''}
                        onChange={e => updateWorkflowTemp(idx, parseFloat(e.target.value))}
                        placeholder="默认" />
                    </label>
                    <button className="btn-icon wf-remove" onClick={() => removeWorkflowStep(idx)}><Icon name="x" size={16} /></button>
                  </div>
                ))}
                <button className="btn-secondary" onClick={addWorkflowStep}>+ 添加步骤</button>
              </div>

            <div className="form-field">
              <label>提示词模板</label>
              {Object.entries(skillEditor.prompts).map(([key, val]) => (
                <div key={key} className="prompt-editor-item">
                  <div className="prompt-editor-header">
                    <input className="input prompt-key-input" value={key} onChange={e => updatePromptKey(key, e.target.value)} placeholder="键名" />
                    <button className="btn-icon" onClick={() => removePrompt(key)}><Icon name="x" size={16} /></button>
                  </div>
                  <textarea className="input" rows={3} value={val} onChange={e => updatePromptValue(key, e.target.value)} placeholder="提示词内容，可用 {{变量}} 插入上下文..." />
                </div>
              ))}
              <button className="btn-secondary" onClick={addPrompt}>+ 添加提示词</button>
            </div>

            <div className="form-row" style={{justifyContent:'flex-end',marginTop:16}}>
              <button className="btn-ghost" onClick={() => setShowSkillEditor(false)}>取消</button>
              <button className="btn-primary" onClick={handleSaveSkill} disabled={skillSaving}>
                {skillSaving ? '保存中...' : '保存技能包'}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* 【需求1-1：双击预览】技能包预览 Modal */}
      {previewPack && (
        <div className="skill-editor-overlay" onClick={() => setPreviewPack(null)}>
          <div className="skill-editor-modal" onClick={e => e.stopPropagation()} style={{ maxWidth: 760 }}>
            <div className="skill-editor-header">
              <h3 style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                <Icon name={resolveIcon(previewPack.icon)} size={22} />
                <span>{previewPack.name}</span>
                {previewPack.is_builtin ? <span className="builtin-badge">系统</span> : <span className="custom-badge">自定义</span>}
                <span style={{
                  fontSize: 11, fontWeight: 500, padding: '2px 8px', borderRadius: 999,
                  background: (previewPack.category || 'master') === 'master' ? 'rgba(74,144,217,0.12)'
                    : previewPack.category === 'style' ? 'rgba(217,119,6,0.12)' : 'rgba(5,150,105,0.12)',
                  color: (previewPack.category || 'master') === 'master' ? '#4a90d9'
                    : previewPack.category === 'style' ? '#d97706' : '#059669'
                }}>
                  {(previewPack.category || 'master') === 'master' ? '构思类' : previewPack.category === 'style' ? '文风类' : '审查类'}
                </span>
              </h3>
              <button className="btn-icon" onClick={() => setPreviewPack(null)}><Icon name="x" size={16} /></button>
            </div>

            <div style={{ padding: '0 4px 12px', fontSize: 12, color: 'var(--text-secondary)' }}>
              {GENRES[previewPack.genre] || previewPack.genre} · {previewPack.book_type === 'novel' ? '长篇' : '短篇'}
              {previewPack.github_source && <> · <Icon name="link" size={13} /> <a href={previewPack.github_source} target="_blank" rel="noreferrer" style={{ color: 'var(--link)' }}>GitHub 源</a></>}
            </div>

            <div style={{ background: 'var(--bg-tertiary)', padding: 12, borderRadius: 8, marginBottom: 16 }}>
              <div style={{ fontSize: 13, lineHeight: 1.7, color: 'var(--text-primary)' }}>{previewPack.description || '暂无描述'}</div>
            </div>

            <div className="form-field">
              <label>工作流步骤（{previewPack.workflow?.length || 0}步）</label>
              <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
                {!previewPack.workflow?.length && <div className="text-muted" style={{fontSize:12}}>暂无工作流步骤</div>}
                {previewPack.workflow?.map((step, i) => (
                  <div key={i} style={{
                    display: 'flex', alignItems: 'flex-start', gap: 10,
                    padding: '8px 10px', background: 'var(--bg-tertiary)', borderRadius: 6
                  }}>
                    <span style={{
                      flexShrink:0, width: 22, height:22, borderRadius:'50%',
                      background:'var(--accent)', color:'#fff', fontSize:11,
                      display:'flex',alignItems:'center',justifyContent:'center', fontWeight:600
                    }}>{step.step || i+1}</span>
                    <div style={{ flex: 1, minWidth: 0 }}>
                      <div style={{ fontSize: 13, fontWeight: 600, color: 'var(--text-primary)' }}>{step.name || '未命名'}</div>
                      {step.desc && <div style={{ fontSize: 11, color: 'var(--text-secondary)', marginTop: 2 }}>{step.desc}</div>}
                      {step.prompt_key && (
                        <div style={{
                          fontSize: 11, color: 'var(--accent)', marginTop: 4,
                          fontFamily: 'var(--mono)', background: 'var(--bg-secondary)',
                          padding: '2px 6px', borderRadius: 4, display: 'inline-block'
                        }}>
                          <Icon name="key" size={11} /> {step.prompt_key}
                        </div>
                      )}
                    </div>
                  </div>
                ))}
              </div>
            </div>

            <div className="form-field">
              <label>提示词模板（{Object.keys(previewPack.prompts || {}).length}个）</label>
              <div style={{ display: 'flex', flexDirection: 'column', gap: 10, maxHeight: 260, overflow: 'auto' }}>
                {!Object.keys(previewPack.prompts || {}).length && <div className="text-muted" style={{fontSize:12}}>暂无提示词模板</div>}
                {Object.entries(previewPack.prompts || {}).map(([key, val]) => (
                  <div key={key} style={{
                    background: 'var(--bg-tertiary)', borderRadius: 6, overflow: 'hidden',
                    border: '1px solid var(--border)'
                  }}>
                    <div style={{
                      padding: '6px 10px', background: 'var(--bg-secondary)',
                      fontSize: 12, fontWeight: 600, fontFamily: 'var(--mono)',
                      borderBottom: '1px solid var(--border)', color: 'var(--accent)'
                    }}>
                      <Icon name="file-text" size={12} /> {key}
                    </div>
                    <pre style={{
                      margin: 0, padding: 10, fontSize: 12, lineHeight: 1.7,
                      color: 'var(--text-primary)', whiteSpace: 'pre-wrap',
                      wordBreak: 'break-word', fontFamily: 'var(--mono)',
                      maxHeight: 160, overflow: 'auto'
                    }}>{val || '（空）'}</pre>
                  </div>
                ))}
              </div>
            </div>

            <div className="form-row" style={{ justifyContent: 'flex-end', marginTop: 8, gap: 8 }}>
              {previewPack.is_builtin ? (
                <button className="btn-secondary" onClick={() => { setPreviewPack(null); handleEditBuiltinSkill(previewPack); }}><Icon name="edit-pencil" size={14} /> 编辑副本</button>
              ) : (
                <button className="btn-secondary" onClick={() => { setPreviewPack(null); openEditSkill(previewPack); }}><Icon name="edit-pencil" size={14} /> 编辑</button>
              )}
              <button className="btn-primary" onClick={() => setPreviewPack(null)}>关闭</button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}