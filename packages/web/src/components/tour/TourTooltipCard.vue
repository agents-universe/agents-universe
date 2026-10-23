<template>
  <div class="tour-tooltip-inner">
    <div class="tour-tooltip-title">{{ t(step.titleKey) }}</div>
    <div class="tour-tooltip-body">{{ t(step.bodyKey) }}</div>
    <div v-if="step.choices" class="tour-choices">
      <button
        v-for="choice in step.choices"
        :key="choice.value"
        class="tour-choice-btn"
        :class="{ 'tour-choice-btn--active': locale === choice.value }"
        @click="chooseLocale(choice.value)"
      >
        {{ choice.label }}
      </button>
    </div>
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
import { setLocale, type AppLocale } from '@/i18n'
import { patchPreferences } from '@/api/preferences'

defineProps<{ step: TourStep }>()

const tour = useTourStore()
const { t, locale } = useI18n()

const isLast = computed(() => tour.stepIndex >= TOUR_STEPS.length - 1)

function chooseLocale(value: AppLocale) {
  setLocale(value)
  // Optimistic like the tour store's completion patch: a failed write just
  // means the choice does not follow the user to another device.
  patchPreferences({ locale: value }).catch((err) => {
    console.warn('tour: failed to persist locale', err)
  })
  void tour.next()
}
</script>
