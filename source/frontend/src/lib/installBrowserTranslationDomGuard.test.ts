/**
 * installBrowserTranslationDomGuard 单测（vitest，零 jsdom 依赖）：
 * 用 stub 的假 Node 类验证守卫语义——
 *   正常父子删除/插入  → 透传原生行为
 *   翻译扩展移位后删除  → 不抛 NotFoundError，返回被删节点
 *   参照节点被挪走后插入 → 降级 append 而非抛错
 * 运行：npm test
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { installBrowserTranslationDomGuard } from './installBrowserTranslationDomGuard';

/** 模拟原生 Node 语义（parent 校验失败时抛 NotFoundError，与浏览器一致）。 */
class FakeNode {
  parentNode: FakeNode | null = null;
  children: FakeNode[] = [];
  appendChild<T extends FakeNode>(child: T): T {
    child.parentNode = this;
    this.children.push(child);
    return child;
  }
  removeChild<T extends FakeNode>(child: T): T {
    if (child.parentNode !== this) {
      throw new DOMException('The node to be removed is not a child of this node.', 'NotFoundError');
    }
    child.parentNode = null;
    this.children = this.children.filter(c => c !== child);
    return child;
  }
  insertBefore<T extends FakeNode>(newNode: T, ref: FakeNode | null): T {
    if (ref && ref.parentNode !== this) {
      throw new DOMException('The node before which the new node is to be inserted is not a child of this node.', 'NotFoundError');
    }
    if (!ref) return this.appendChild(newNode);
    const i = this.children.indexOf(ref);
    this.children.splice(i, 0, newNode);
    newNode.parentNode = this;
    return newNode;
  }
}

beforeEach(() => {
  vi.stubGlobal('Node', FakeNode);
  delete (FakeNode.prototype as unknown as Record<string, unknown>).__translationGuarded__;
});

describe('installBrowserTranslationDomGuard', () => {
  it('正常父子删除：透传原生行为', () => {
    installBrowserTranslationDomGuard();
    const parent = new FakeNode();
    const child = new FakeNode();
    parent.appendChild(child);
    expect(parent.removeChild(child)).toBe(child);
    expect(child.parentNode).toBeNull();
  });

  it('删除非子节点（线上崩溃场景）：不抛错并返回该节点', () => {
    installBrowserTranslationDomGuard();
    const parent = new FakeNode();
    const stranger = new FakeNode(); // 从未挂到 parent 上（被"翻译器"移走）
    expect(() => parent.removeChild(stranger)).not.toThrow();
    expect(parent.removeChild(stranger)).toBe(stranger);
  });

  it('参照节点是亲生子节点：正常插入其前', () => {
    installBrowserTranslationDomGuard();
    const parent = new FakeNode();
    const ref = new FakeNode();
    const inserted = new FakeNode();
    parent.appendChild(ref);
    parent.insertBefore(inserted, ref);
    expect(parent.children[0]).toBe(inserted);
    expect(parent.children[1]).toBe(ref);
  });

  it('参照节点已被挪到别家（线上崩溃场景）：降级 append 而非抛错', () => {
    installBrowserTranslationDomGuard();
    const parent = new FakeNode();
    const otherParent = new FakeNode();
    const ref = new FakeNode();
    const inserted = new FakeNode();
    otherParent.appendChild(ref); // ref 的父节点不是 parent
    expect(() => parent.insertBefore(inserted, ref)).not.toThrow();
    expect(parent.children).toContain(inserted);
  });

  it('幂等：重复安装不二次包裹', () => {
    installBrowserTranslationDomGuard();
    const after = FakeNode.prototype.removeChild;
    installBrowserTranslationDomGuard();
    expect(FakeNode.prototype.removeChild).toBe(after);
  });
});
