<template>
  <Teleport to="body">
    <div v-if="tour.whatsNewVisible" class="modal-overlay" ref="overlayEl" @keydown.esc="close">
      <div class="modal-dialog whats-new-dialog" role="dialog" aria-modal="true">
        <div class="modal-header">
          <h3 class="modal-title">{{ t('whatsNew.title') }}</h3>
          <button class="modal-close" :title="t('common.close')" @click="close">
            <X :size="16" />
          </button>
        </div>
        <div class="modal-body">
          <div v-if="entries.length === 0" class="whats-new-empty">{{ t('whatsNew.empty') }}</div>
          <div v-for="entry in entries" :key="entry.version" class="whats-new-entry">
            <div class="whats-new-entry-meta">
              <span class="whats-new-entry-version">
                {{ t('whatsNew.version', { version: entry.version }) }}
              </span>
              <span class="whats-new-entry-date">{{ t('whatsNew.releasedOn', { date: entry.date }) }}</span>
            </div>
            <div class="whats-new-entry-title">{{ t(entry.titleKey) }}</div>
            <ul class="whats-new-entry-features">
              <li v-for="key in entry.featureKeys" :key="key">{{ t(key) }}</li>
            </ul>
          </div>
        </div>
        <div class="modal-actions">
          <button class="btn-primary" @click="close">{{ t('whatsNew.close') }}</button>
        </div>
      </div>
    </div>
  </Teleport>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { X } from 'lucide-vue-next'
import { useTourStore } from '@/stores/tour'
import { RELEASES } from '@/tour/releases'
import { entriesToShow } from '@/tour/whatsNew'
import { useClickOutside } from '@/composables/useClickOutside'

const tour = useTourStore()
const { t } = useI18n()

const entries = computed(() => entriesToShow(tour.lastSeenVersion, RELEASES))

function close() {
  void tour.dismissWhatsNew()
}

const overlayEl = ref<HTMLElement | null>(null)
useClickOutside(overlayEl, close, true)
</script>
