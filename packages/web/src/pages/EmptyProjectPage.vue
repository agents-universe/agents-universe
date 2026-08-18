<template>
  <div class="empty-project-page">
    <div class="empty-project-content">
      <div class="empty-project-logo">
        <span class="empty-project-logo-icon">
          <Bot :size="36" />
        </span>
        <h1 class="empty-project-brand">Agents Universe</h1>
        <p class="empty-project-tagline">企业级 AI Agent 框架</p>
      </div>

      <div class="feature-cards">
        <div class="feature-card">
          <span class="feature-card-icon">
            <MessageSquare :size="20" />
          </span>
          <div class="feature-card-body">
            <h3 class="feature-card-title">智能对话</h3>
            <p class="feature-card-desc">与 AI Agent 进行多轮对话，支持流式输出与工具调用</p>
          </div>
        </div>

        <div class="feature-card">
          <span class="feature-card-icon">
            <BookOpen :size="20" />
          </span>
          <div class="feature-card-body">
            <h3 class="feature-card-title">知识条目</h3>
            <p class="feature-card-desc">为每个项目维护独立的知识文档，精准注入上下文</p>
          </div>
        </div>

        <div class="feature-card">
          <span class="feature-card-icon">
            <Brain :size="20" />
          </span>
          <div class="feature-card-body">
            <h3 class="feature-card-title">记忆系统</h3>
            <p class="feature-card-desc">跨会话持久化个人偏好与情节记忆，越用越聪明</p>
          </div>
        </div>
      </div>

      <div class="empty-project-cta">
        <p class="empty-project-hint">从左侧选择已有项目，或立即创建一个新项目开始使用</p>
        <button class="btn-primary empty-project-btn" @click="showCreate = true">
          <Plus :size="16" />
          新建项目
        </button>
      </div>
    </div>

    <CreateProjectDialog v-if="showCreate" @close="showCreate = false" />
  </div>
</template>

<script setup lang="ts">
import { ref } from 'vue'
import { Bot, MessageSquare, BookOpen, Brain, Plus } from 'lucide-vue-next'
import CreateProjectDialog from '@/components/sidebar/CreateProjectDialog.vue'

const showCreate = ref(false)
</script>
