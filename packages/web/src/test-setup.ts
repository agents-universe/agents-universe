import './test-env'
import { config } from '@vue/test-utils'
import { i18n } from './i18n/index'

// Mounted components use useI18n(); inject the shared instance globally
// so every mount() in tests has translations without per-test boilerplate.
config.global.plugins = [i18n]
