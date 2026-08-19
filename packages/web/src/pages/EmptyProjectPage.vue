<template>
  <div class="empty-project-page">
    <div class="empty-project-content">
      <div class="empty-project-logo">
        <span class="empty-project-logo-icon">
          <Bot :size="36" />
        </span>
        <h1 class="empty-project-brand">Agents Universe</h1>
        <p class="empty-project-tagline">{{ t('emptyProject.tagline') }}</p>
      </div>

      <div class="feature-cards">
        <div class="feature-card">
          <span class="feature-card-icon">
            <MessageSquare :size="20" />
          </span>
          <div class="feature-card-body">
            <h3 class="feature-card-title">{{ t('emptyProject.featureChatTitle') }}</h3>
            <p class="feature-card-desc">{{ t('emptyProject.featureChatDesc') }}</p>
          </div>
        </div>

        <div class="feature-card">
          <span class="feature-card-icon">
            <BookOpen :size="20" />
          </span>
          <div class="feature-card-body">
            <h3 class="feature-card-title">{{ t('emptyProject.featureKnowledgeTitle') }}</h3>
            <p class="feature-card-desc">{{ t('emptyProject.featureKnowledgeDesc') }}</p>
          </div>
        </div>

        <div class="feature-card">
          <span class="feature-card-icon">
            <Brain :size="20" />
          </span>
          <div class="feature-card-body">
            <h3 class="feature-card-title">{{ t('emptyProject.featureMemoryTitle') }}</h3>
            <p class="feature-card-desc">{{ t('emptyProject.featureMemoryDesc') }}</p>
          </div>
        </div>
      </div>

      <div class="empty-project-cta">
        <p class="empty-project-hint">{{ t('emptyProject.hint') }}</p>
        <button class="btn-primary empty-project-btn" @click="showCreate = true">
          <Plus :size="16" />
          {{ t('emptyProject.createProject') }}
        </button>
      </div>
    </div>

    <CreateProjectDialog v-if="showCreate" @close="showCreate = false" />
  </div>
</template>

<script setup lang="ts">
import { ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { Bot, MessageSquare, BookOpen, Brain, Plus } from 'lucide-vue-next'
import CreateProjectDialog from '@/components/sidebar/CreateProjectDialog.vue'

const { t } = useI18n()
const showCreate = ref(false)
</script>
