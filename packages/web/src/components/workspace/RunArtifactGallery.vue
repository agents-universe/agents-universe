<template>
  <div v-if="hasContent" class="run-artifacts">
    <div class="run-result-section">
      {{ t('workspace.artifactsTitle') }}
      <button v-if="reportUrl" class="btn-ghost run-report-btn" @click="openReport">
        <ExternalLink :size="13" /> {{ t('workspace.openReport') }}
      </button>
    </div>

    <div v-if="screenshots.length" class="run-artifact-shots">
      <figure v-for="item in screenshots" :key="item.rel_path" class="run-shot">
        <a :href="url(item)" target="_blank" rel="noopener noreferrer">
          <img :src="url(item)" :alt="item.test_title || item.name" loading="lazy" />
        </a>
        <figcaption>{{ item.test_title || item.name }}</figcaption>
      </figure>
    </div>

    <div v-for="item in videos" :key="item.rel_path" class="run-video">
      <video controls preload="metadata" :src="url(item)" />
      <div class="run-artifact-name">{{ item.test_title || item.name }}</div>
    </div>

    <div v-if="files.length" class="run-artifact-files">
      <a
        v-for="item in files"
        :key="item.rel_path"
        class="run-artifact-file"
        :href="url(item)"
        :download="item.name"
      >
        <Paperclip :size="12" />
        <span class="run-artifact-name">{{ item.test_title || item.name }}</span>
        <span class="run-artifact-size">{{ humanSize(item.size_bytes) }}</span>
      </a>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import { ExternalLink, Paperclip } from 'lucide-vue-next'
import { artifactPath, type ArtifactEntry } from '@/api/scripts'
import { withApi } from '@/utils/basePath'

const props = defineProps<{
  runId: string
  artifacts: ArtifactEntry[]
  reportUrl: string | null
}>()

const { t } = useI18n()

// The manifest carries the HTML report as an entry too; it is reachable
// through the sandboxed report URL below, never as a plain download.
const usable = computed(() => props.artifacts.filter((a) => a.kind !== 'report'))
const screenshots = computed(() => usable.value.filter((a) => a.kind === 'screenshot'))
const videos = computed(() => usable.value.filter((a) => a.kind === 'video'))
const files = computed(() =>
  usable.value.filter((a) => a.kind !== 'screenshot' && a.kind !== 'video'),
)
const hasContent = computed(() => usable.value.length > 0 || !!props.reportUrl)

function url(item: ArtifactEntry): string {
  return withApi(artifactPath(props.runId, item.rel_path))
}

function openReport(): void {
  if (!props.reportUrl) return
  // A new tab, not an iframe: the report is served with a CSP sandbox, so it
  // cannot reach this page's session even when opened alongside it.
  window.open(withApi(props.reportUrl), '_blank', 'noopener,noreferrer')
}

function humanSize(bytes: number): string {
  if (!bytes) return ''
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`
}
</script>
