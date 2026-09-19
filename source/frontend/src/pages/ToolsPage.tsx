import { useState, useEffect } from 'react';
import { api } from '../api';
import type { Book } from '../types';
import ReviewTab from './tools/ReviewTab';
import SkillsTab from './tools/SkillsTab';
import AnalyzeTab from './tools/AnalyzeTab';
import RankingsTab from './tools/RankingsTab';

type ToolTab = 'review' | 'skills' | 'analyze' | 'rankings';

const TOOL_TABS: { key: ToolTab; label: string; icon: string; desc: string }[] = [
  { key: 'review', label: 'AI 责编', icon: '🔍', desc: 'AI平台视角审稿打分' },
  { key: 'skills', label: '技能包', icon: '📦', desc: '15+题材工作流套件' },
  { key: 'analyze', label: '拆书分析', icon: '📊', desc: '导入文件分析提炼方法论' },
  { key: 'rankings', label: '榜单风向', icon: '📈', desc: '各平台排行榜趋势洞察' },
];

export default function ToolsPage() {
  const [activeTab, setActiveTab] = useState<ToolTab>('review');
  const [books, setBooks] = useState<Book[]>([]);
  const [selectedBookId, setSelectedBookId] = useState('');
  // #3 移动端自动折叠：工具箱 + 选择作品 在 activeTab=rankings 时收起
  const [toolsCollapsedMobile, setToolsCollapsedMobile] = useState<boolean | null>(null); // null=未初始化

  // 工具类型 Tab 切换 → 移动端自动展开/折叠工具箱与作品选择
  useEffect(() => {
    const isMobile = typeof window !== 'undefined' && window.matchMedia && window.matchMedia('(max-width: 767px)').matches;
    if (!isMobile) { setToolsCollapsedMobile(false); return; }
    if (activeTab === 'rankings') {
      // 需求 3：手机端点击榜单风向，选择作品上面的（工具箱 + 选择作品）自动折叠
      setToolsCollapsedMobile(true);
    } else if (toolsCollapsedMobile !== false) {
      setToolsCollapsedMobile(false);
    }
  }, [activeTab]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    api.listBooks().then(setBooks).catch(() => {});
  }, []);

  return (
    <div className="page tools-page">
      <header className="page-header">
        <h1>工具箱</h1>
      </header>

      {/* #3 手机端：工具箱 + 选择作品 = 可折叠面板（进榜单风向时自动收起） */}
      <section className={`tools-top-section nr-collapsible ${toolsCollapsedMobile ? 'is-collapsed' : ''}`}
               data-collapsed={toolsCollapsedMobile ? '1' : '0'}>
        <button
          type="button"
          className="nr-collapse-toggle"
          aria-expanded={!toolsCollapsedMobile}
          onClick={() => setToolsCollapsedMobile((v: boolean | null) => !(v === true))}
        >
          <span className="nr-collapse-toggle__label">
            {toolsCollapsedMobile ? '▽ 展开 工具箱 & 选择作品' : '△ 收起 工具箱 & 选择作品'}
          </span>
          <span className="nr-collapse-toggle__icon">{toolsCollapsedMobile ? '＋' : '－'}</span>
        </button>
        <div className="nr-collapsible-body">
          <div className="tools-grid">
            {TOOL_TABS.map(tab => (
              <button key={tab.key} className={`tool-card ${activeTab === tab.key ? 'active' : ''}`} onClick={() => setActiveTab(tab.key)}>
                <span className="tool-card-icon">{tab.icon}</span>
                <div className="tool-card-info">
                  <div className="tool-card-name">{tab.label}</div>
                  <div className="tool-card-desc">{tab.desc}</div>
                </div>
              </button>
            ))}
          </div>

          <div className="form-row nr-select-book-row" style={{marginTop:12}}>
            <label className="input-label">选择作品</label>
            <select className="input" value={selectedBookId} onChange={e => setSelectedBookId(e.target.value)}>
              <option value="">— 选择要操作的作品 —</option>
              {books.map(b => <option key={b.id} value={b.id}>{b.title} ({b.word_count}字)</option>)}
            </select>
          </div>
        </div>
      </section>

      {activeTab === 'review' && <ReviewTab selectedBookId={selectedBookId} />}
      {activeTab === 'skills' && <SkillsTab selectedBookId={selectedBookId} />}
      {activeTab === 'analyze' && <AnalyzeTab books={books} />}
      {activeTab === 'rankings' && <RankingsTab />}
    </div>
  );
}