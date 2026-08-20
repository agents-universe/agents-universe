<template>
  <div class="tour-tooltip-inner">
    <div class="tour-tooltip-title">{{ t(step.titleKey) }}</div>
    <div class="tour-tooltip-body">{{ t(step.bodyKey) }}</div>
    <div class="tour-tooltip-actions">
      <button class="tour-skip-btn" @click="tour.skip()">{{ t('tour.skip') }}</button>
      <span class="tour-progress">
        {{ t('tour.stepOf', { current: tour.stepIndex + 1, total: TOUR_STEPS.length }) }}
      </span>
      <button
        class="btn-primary tour-next-btn"
        :disabled="tour.waiting"
        @click="isLast ? tour.finish() : tour.next()"
      >
        {{ tour.waiting ? t('tour.waiting') : t(isLast ? 'tour.doneBtn' : 'tour.next') }}
      </button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import { useTourStore } from '@/stores/tour'
import { TOUR_STEPS, type TourStep } from '@/tour/steps'

defineProps<{ step: TourStep }>()

const tour = useTourStore()
const { t } = useI18n()

const isLast = computed(() => tour.stepIndex >= TOUR_STEPS.length - 1)
</script>
