// Runs before anything imports @/i18n: pin the detected locale so
// assertions on default-locale copy stay deterministic regardless of the
// machine's navigator.language.
localStorage.setItem('au:locale', 'zh-CN')
