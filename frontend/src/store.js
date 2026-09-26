export const DEMO_KEY = 'knowpath-demo-v1';
export const MODE_KEY = 'knowpath-mode';
export const TOKEN_KEY = 'knowpath-session-token';
export const esc = value => String(value ?? '').replace(/[&<>"']/g, char => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[char]);
export const uid = () => globalThis.crypto.randomUUID();
export const dateText = date => {
  const value = new Date(date);
  return Number.isNaN(value.getTime()) ? '—' : new Intl.DateTimeFormat('zh-CN', { month: 'short', day: 'numeric' }).format(value);
};
export const sizeText = bytes => !Number.isFinite(bytes) ? '—' : bytes < 1024 ? `${bytes} B` : bytes < 1048576 ? `${(bytes / 1024).toFixed(0)} KB` : `${(bytes / 1048576).toFixed(1)} MB`;
export function storageRead(storage, key, fallback = null) {
  try { const raw = storage.getItem(key); return raw == null ? fallback : JSON.parse(raw); } catch { return fallback; }
}
export function storageWrite(storage, key, value) {
  try { storage.setItem(key, JSON.stringify(value)); return true; } catch { return false; }
}
export function validateFile(file) {
  if (!/\.(pdf|md|txt)$/i.test(file.name)) throw new Error('请选择 PDF、Markdown（.md）或 TXT 文件。');
  if (file.size > 20 * 1024 * 1024) throw new Error('单个文件不能超过 20 MB。');
  if (file.size === 0) throw new Error('这个文件是空的，请选择有内容的资料。');
}
const names = [
 ['线性代数', '向量与空间', '线性组合', '矩阵运算', '线性变换', '特征值', '特征向量', '正交与投影'],
 ['Python 编程', '基础语法', '变量与类型', '流程控制', '函数', '面向对象', '文件读写'],
 ['机器学习', '监督学习', '线性回归', '梯度下降', '模型评估', '过拟合与正则化'],
];
export function makeDemo(now = new Date()) {
  const timestamp = now.toISOString();
  const spaces = [
    { id: 'demo-linear', name: '线性代数', goal: '从几何直觉出发，理解数学的语言。', theme: 'blue', material_ids: ['demo-m1', 'demo-m2'], weekly_minutes: 150 },
    { id: 'demo-python', name: 'Python 编程', goal: '让想法落地，从第一行代码开始。', theme: 'sage', material_ids: ['demo-m3'], weekly_minutes: 120 },
    { id: 'demo-ml', name: '机器学习入门', goal: '走近数据背后的规律与可能。', theme: 'lavender', material_ids: ['demo-m4'], weekly_minutes: 90 },
  ].map((s, i) => ({ ...s, status: 'active', created_at: timestamp, topic_ids: names[i].map((_, j) => `${s.id}-t${j}`), space_version: 1, profile_version: 1,
    nodes: names[i].map((label, j) => ({ id: `${s.id}-t${j}`, title: label, status: j > 0 && j < 4 ? 'mastered' : j === 4 ? 'learning' : 'new', description: j === 0 ? s.goal : `从「${label}」出发，梳理概念之间的联系，并通过练习加深理解。`, source_refs: [{ material_id: s.material_ids[0] }] })),
    edges: names[i].slice(1).map((_, j) => ({ id: `${s.id}-e${j}`, from_id: `${s.id}-t${j > 3 ? 3 : 0}`, to_id: `${s.id}-t${j + 1}`, type: 'contains' })),
  }));
  const materials = [
    { id: 'demo-m1', name: '线性代数 · 概念与直觉.md', type: 'md', size_bytes: 24350, text: '# 线性代数：从直觉到理解\n\n## 向量与空间\n向量可以理解为带有方向和大小的量。二维向量 (x, y) 描述平面上的一个点或一次位移。\n\n## 线性组合\n向量 u、v 的线性组合是 a·u + b·v。通过改变系数 a、b，我们得到这两个向量张成的空间。\n\n## 矩阵与线性变换\n矩阵可以描述一种保持向量加法和数乘的变换。矩阵的各列是标准基向量经过变换后的像。\n\n## 特征向量\n某些非零向量经过变换后仍与原方向共线，它们就是特征向量。满足 Av = λv，其中 λ 是对应的特征值。\n\n这是一份内置示例笔记，用于体验前端交互。' },
    { id: 'demo-m2', name: '矩阵运算 · 练习笔记.txt', type: 'txt', size_bytes: 8650, text: '矩阵运算练习\n\n1. 设 A = [[1, 2], [0, 1]]，求向量 (1, 1) 变换后的结果。\n2. 解释单位矩阵为何不会改变向量。\n3. 比较 AB 和 BA：矩阵乘法一定满足交换律吗？\n\n参考思路：先对每一行进行点积，再用坐标变化检查结果。\n\n内置示例内容。' },
    { id: 'demo-m3', name: 'Python 学习手册.md', type: 'md', size_bytes: 32760, text: '# Python 学习手册\n\n## 变量\n变量用于命名和引用一个值。\n\n## 函数\n使用 def 定义可复用的逻辑。参数描述输入，return 描述返回值。\n\ndef greet(name):\n    return f"你好，{name}"\n\n## 练习\n写一个函数，计算列表中所有数字的平均值，并处理空列表。\n\n内置示例内容。' },
    { id: 'demo-m4', name: '机器学习 · 第一课.md', type: 'md', size_bytes: 18620, text: '# 机器学习入门\n\n## 监督学习\n利用带有标签的样本学习输入到输出的映射。\n\n## 线性回归\n用线性函数预测连续数值，通过最小化损失调整参数。\n\n## 梯度下降\n沿着损失函数梯度的反方向逐步更新参数。学习率控制每一步的大小。\n\n## 模型评估\n将训练数据和测试数据分开，以评估对未见样本的泛化能力。\n\n内置示例内容。' },
  ].map(m => ({ ...m, status: 'ready', created_at: timestamp, version: 1 }));
  const tasks = [
    { id: 'demo-task-1', space_id: 'demo-linear', title: '理解矩阵与线性变换', type: 'learn', minutes: 25, status: 'pending', topic_ids: ['demo-linear-t4'] },
    { id: 'demo-task-2', space_id: 'demo-python', title: '温习函数与参数', type: 'review', minutes: 15, status: 'pending', topic_ids: ['demo-python-t4'] },
    { id: 'demo-task-3', space_id: 'demo-linear', title: '完成向量的线性组合练习', type: 'practice', minutes: 10, status: 'completed', topic_ids: ['demo-linear-t2'] },
  ];
  return { version: 1, spaces, materials, tasks };
}
export function loadDemo(storage) {
  const value = storageRead(storage, DEMO_KEY);
  if (value?.version === 1 && Array.isArray(value.spaces) && Array.isArray(value.materials) && Array.isArray(value.tasks)
    && value.spaces.every(s => typeof s.id === 'string' && typeof s.name === 'string' && Array.isArray(s.nodes) && Array.isArray(s.edges))
    && value.materials.every(m => typeof m.id === 'string' && typeof m.name === 'string')
    && value.tasks.every(t => typeof t.id === 'string' && typeof t.title === 'string')) return value;
  return makeDemo();
}
export function spaceProgress(space) {
  const nodes = space.nodes || [];
  if (!nodes.length) return null;
  return Math.round(nodes.filter(n => n.status === 'mastered').length / nodes.length * 100);
}
export function materialIds(space) { return space.material_ids || (space.bindings || []).map(b => b.material_id); }
