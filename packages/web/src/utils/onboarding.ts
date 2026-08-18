/**
 * Onboarding kickoff messages, branched by project category.
 * 「其他」分类项目知识条目最少,由项目定制专家访谈后填充。
 */
export const CUSTOMIZATION_EXPERT_SLUG = 'project-customization-expert'

export const ONBOARDING_KICKOFF = [
  'This is a newly created project. Run project onboarding:',
  '1. Check existing project-scoped personal memories — only ask for items not already saved.',
  '2. Ask for non-secret customer/project config (issue tracker key, source control host, API base URLs, etc.) and save each as project_setting memory.',
  '3. For secrets/tokens, use secure prompts (user_confirm with secret=true) that save to project secrets — never ask for plaintext.',
  '4. For third-party API integrations, help the user populate integrations/custom-api knowledge with endpoint catalogs.',
  '5. Do NOT write customer info into framework templates or knowledge files — only shared durable project facts go into project knowledge.',
].join('\n')

export const OTHER_ONBOARDING_KICKOFF = [
  '这是一个「其他」分类的新建项目，仅有最基础的知识条目（项目背景与历史）。',
  '请以「项目定制专家」身份启动知识定制访谈：',
  '1. 先读取当前知识文件与项目记忆，识别缺失项。',
  '2. 围绕业务领域、参与者、关键系统与工具、智能体职责做开放式访谈（每轮 3-5 个问题）。',
  '3. 访谈后创建自定义知识文件（slug 格式 {category}/{kebab-case-name}），不适用知识条目标记 status: not_applicable 而非删除。',
  '4. 完成后汇报写入位置与 completeness 分数变化。',
].join('\n')

export function buildOnboardingKickoff(category?: string | null): string {
  return category === 'other' ? OTHER_ONBOARDING_KICKOFF : ONBOARDING_KICKOFF
}
