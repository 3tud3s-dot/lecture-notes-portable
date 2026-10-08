import index from '../../references/index.json'

export type Course = { course_id: string; display_name: string; reference_path: string }
export const courses: Course[] = index

// Presentation and demo copy only; the course identity always comes from the catalog.
export const coursePresentation: Record<string, { english: string; topic: string; glyph: string }> = {
  microcomputer: { english: 'MICROCOMPUTER PRINCIPLES', topic: '微型计算机系统 · 接口与控制', glyph: 'chip' },
  optimization: { english: 'INTRODUCTION TO OPTIMIZATION', topic: '最优解 · 约束与迭代方法', glyph: 'chart' },
  pattern_recognition: { english: 'PATTERN RECOGNITION & MACHINE LEARNING', topic: '从数据中发现规律', glyph: 'nodes' },
  modern_control: { english: 'MODERN CONTROL THEORY', topic: '状态空间 · 能控性与能观性', glyph: 'sliders' },
  power_electronics: { english: 'POWER ELECTRONICS', topic: '电能变换 · 器件与电路', glyph: 'bolt' },
}

export function timestamp(seconds: number) {
  return `${Math.floor(seconds / 60).toString().padStart(2, '0')}:${(seconds % 60).toString().padStart(2, '0')}`
}
