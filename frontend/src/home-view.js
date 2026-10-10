const rows = value => Array.isArray(value) ? value : [];

export function currentSpaceMaterialIds(space) {
  return rows(space?.bindings).map(binding => binding.material_id).filter(Boolean);
}

export function sortHomeMaterials(materials, space) {
  const ids = new Set(currentSpaceMaterialIds(space));
  return rows(materials)
    .filter(material => ids.has(material.id))
    .sort((left, right) => {
      const rightTime = Date.parse(right.created_at || '') || 0;
      const leftTime = Date.parse(left.created_at || '') || 0;
      return rightTime - leftTime;
    });
}

export function homePlanState(plan) {
  if (!plan) return { action: '生成学习计划', headline: '先安排一条学习路线。' };
  if (plan.status === 'needs_replan') return { action: '查看学习路线', headline: '确认路线，再开始学习。' };
  return { action: '开始学习', headline: '开始下一项学习。' };
}

export function notebookSummary(notebook) {
  const generated = rows(notebook?.chapters)
    .filter(chapter => chapter.generation_status === '已生成' || chapter.generation_status === 'completed')
    .sort((left, right) => (Date.parse(right.updated_at || '') || 0) - (Date.parse(left.updated_at || '') || 0));
  if (!generated.length) return { kind: 'empty', label: '尚未生成学习笔记' };
  const latest = generated[0];
  return {
    kind: 'latest',
    title: latest.title || latest.filename || '未命名章节',
    status: latest.status || 'IN_PROGRESS',
    correctionCount: Number(latest.correction_count || 0),
    updatedAt: latest.updated_at || '',
  };
}
