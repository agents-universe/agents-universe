import { createApp } from 'vue'
import { createPinia } from 'pinia'
import App from './App.vue'
import router from './router'
import './index.css'
import './styles/layout.css'

createApp(App).use(createPinia()).use(router).mount('#root')
