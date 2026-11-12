/** Shared edge presentation for the graph view: colors/line styles (canvas
 * painting + chip swatches) and i18n keys (chips, hover labels, detail rows).
 *
 * One source of truth so GraphPage chips, GraphCanvas painting/hover labels,
 * and GraphDetailPanel `via` rows never disagree. Identity is never
 * color-alone: chips carry labels, hover/selection shows a text label. */

export interface EdgeStyle {
  color: string
  lineStyle?: string
}

/** Mirrors agent_core.knowledge.graph.model EDGE_* constants. Unknown types
 * fall back to the gray default in edgeColor(). */
export const EDGE_STYLE: Record<string, EdgeStyle> = {
  imports: { color: '#4b5563' },
  calls: { color: '#3b82f6' },
  inherits: { color: '#8b5cf6' },
  implements: { color: '#8b5cf6', lineStyle: 'dashed' },
  reads: { color: '#f59e0b' },
  writes: { color: '#f59e0b' },
  foreign_key: { color: '#f59e0b' },
  primary_key: { color: '#f59e0b' },
  indexes: { color: '#f59e0b' },
  references: { color: '#22c55e', lineStyle: 'dashed' },
  case_of: { color: '#22c55e', lineStyle: 'dashed' },
  defines: { color: '#22c55e', lineStyle: 'dashed' },
  listened_by: { color: '#f472b6' },
  triggers: { color: '#22d3ee' },
  executes: { color: '#22d3ee', lineStyle: 'dashed' },
}

/** i18n key per edge type; unmapped types render their raw value. */
export const EDGE_I18N: Record<string, string> = {
  imports: 'graphPage.edgeImports',
  calls: 'graphPage.edgeCalls',
  inherits: 'graphPage.edgeInherits',
  implements: 'graphPage.edgeImplements',
  reads: 'graphPage.edgeReads',
  writes: 'graphPage.edgeWrites',
  foreign_key: 'graphPage.edgeForeignKey',
  primary_key: 'graphPage.edgePrimaryKey',
  references: 'graphPage.edgeReferences',
  case_of: 'graphPage.edgeCaseOf',
  defines: 'graphPage.edgeDefines',
  listened_by: 'graphPage.edgeListenedBy',
  triggers: 'graphPage.edgeTriggers',
  executes: 'graphPage.edgeExecutes',
  indexes: 'graphPage.edgeIndexes',
}

export function edgeColor(type: string): string {
  return EDGE_STYLE[type]?.color ?? '#4b5563'
}
