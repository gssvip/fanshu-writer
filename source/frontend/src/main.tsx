import { Component } from 'react';
import { createRoot } from 'react-dom/client'
import { installBrowserTranslationDomGuard } from './lib/installBrowserTranslationDomGuard'
import './index.css'
import App from './App.tsx'

// 必须在 React 挂载前安装：防止浏览器翻译扩展改写 DOM 后
// react-dom 卸载子树时 removeChild/insertBefore 抛 NotFoundError 崩掉整棵树
installBrowserTranslationDomGuard();

class ErrorBoundary extends Component<{ children: React.ReactNode }, { error: Error | null; info: string }> {
  constructor(props: any) {
    super(props);
    this.state = { error: null, info: '' };
  }
  static getDerivedStateFromError(error: Error) {
    return { error, info: error.message + '\n' + (error.stack || '').split('\n').slice(0, 10).join('\n') };
  }
  componentDidCatch(error: Error, info: any) {
    console.error('ErrorBoundary caught:', error, info);
  }
  render() {
    if (this.state.error) {
      return (
        <div style={{padding:20,fontFamily:'monospace',color:'#e74c3c',whiteSpace:'pre-wrap',wordBreak:'break-all'}}>
          <h2>渲染错误</h2>
          <pre>{this.state.info}</pre>
          <button onClick={() => { this.setState({ error: null, info: '' }); }} style={{marginTop:12,padding:'6px 16px',cursor:'pointer'}}>重试</button>
        </div>
      );
    }
    return this.props.children;
  }
}

// 防御性挂载：先清理 root 节点的所有子元素，避免多 React 实例冲突
const rootEl = document.getElementById('root');
if (rootEl) {
  while (rootEl.firstChild) {
    rootEl.removeChild(rootEl.firstChild);
  }
}
const root = createRoot(rootEl!);

root.render(
  <ErrorBoundary>
    <App />
  </ErrorBoundary>,
)
