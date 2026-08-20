import { createI18n } from 'vue-i18n'
import zhCN from './locales/zh-CN'
import enUS from './locales/en-US'

export type AppLocale = 'zh-CN' | 'en-US'

const STORAGE_KEY = 'au:locale'
const LOCALES: AppLocale[] = ['zh-CN', 'en-US']

function detectLocale(): AppLocale {
  // Locale comes only from an explicit user switch; default to zh-CN when no
  // preference has been saved (browser language is not followed).
  const saved = localStorage.getItem(STORAGE_KEY)
  if (saved && LOCALES.includes(saved as AppLocale)) return saved as AppLocale
  return 'zh-CN'
}

export const i18n = createI18n({
  legacy: false,
  locale: detectLocale(),
  fallbackLocale: 'zh-CN',
  messages: {
    'zh-CN': zhCN,
    'en-US': enUS,
  },
})

// Non-component modules (stores, utils) can't use useI18n(); the composer's
// bound t() is reactive to locale switches, so re-exporting it is safe.
export const t = i18n.global.t

export function setLocale(locale: AppLocale) {
  i18n.global.locale.value = locale
  localStorage.setItem(STORAGE_KEY, locale)
  document.documentElement.lang = locale
}

document.documentElement.lang = i18n.global.locale.value
