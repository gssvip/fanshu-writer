/**
 * 浏览器翻译扩展 DOM 守卫（线上 removeChild 崩溃修复）：
 *
 * 线上报错 NotFoundError: Failed to execute 'removeChild' on 'Node'
 * 堆栈全在 react-dom 提交阶段内部（无业务代码帧）——这是著名的
 * React + 网页翻译交互问题（facebook/react#11538）：
 *
 * 翻译扩展（Google 翻译 / 沉浸式翻译 / Edge 翻译等）会把 React 管理
 * 的文本节点替换为译文并重新挂载片段；React 提交阶段仍持有原始节点
 * 引用，卸载子树时对已被翻译器移走的节点调用 removeChild / 对已被
 * 挪走的参照节点调用 insertBefore，直接抛 NotFoundError 崩掉整棵树。
 *
 * 业界通行缓解（Google 自家应用同款做法）：给这两个原型方法装容错——
 * 节点已不在预期父节点下时静默返回而非抛异常（翻译器实际上已经完成
 * 了等效 DOM 变更）。其余路径原样透传原生实现。
 *
 * 幂等：重复调用安全（HMR 热更新 / 多次挂载场景）。
 */
export function installBrowserTranslationDomGuard() {
  if (typeof Node !== 'function' || !Node.prototype) {
    return;
  }

  const proto = Node.prototype as Node & { __translationGuarded__?: boolean };
  if (proto.__translationGuarded__) {
    return;
  }
  proto.__translationGuarded__ = true;

  const originalRemoveChild = Node.prototype.removeChild;
  Node.prototype.removeChild = function removeChild<T extends Node>(this: Node, child: T): T {
    if (child && child.parentNode !== this) {
      // 节点已被翻译扩展摘走/移位，翻译器已完成等效变更，无需再删
      return child;
    }
    return originalRemoveChild.call(this, child) as T;
  };

  const originalInsertBefore = Node.prototype.insertBefore;
  Node.prototype.insertBefore = function insertBefore<T extends Node>(
    this: Node,
    newNode: T,
    referenceNode: Node | null,
  ): T {
    if (referenceNode && referenceNode.parentNode !== this) {
      // 参照节点已被翻译扩展挪走：降级为 appendChild（最接近 React 意图的安全等价操作）
      return originalInsertBefore.call(this, newNode, null) as T;
    }
    return originalInsertBefore.call(this, newNode, referenceNode) as T;
  };
}
