import { defineStore } from 'pinia'
import { ref } from 'vue'
import type { SessionNote, PersonalMemory, EpisodicMemory } from '@/types'

export const useMemoryStore = defineStore('memory', () => {
  const sessionNotes = ref<SessionNote[]>([])
  const personalMemories = ref<PersonalMemory[]>([])
  const episodes = ref<EpisodicMemory[]>([])
  const refreshToken = ref(0)

  function addSessionNote(note: string) {
    const notes = [...sessionNotes.value, { note, timestamp: Date.now() }]
    sessionNotes.value = notes.slice(-20)
  }

  function clearSessionNotes() {
    sessionNotes.value = []
  }

  function setPersonalMemories(memories: PersonalMemory[]) {
    personalMemories.value = memories
  }

  function addPersonalMemory(memory: PersonalMemory) {
    personalMemories.value = [memory, ...personalMemories.value]
  }

  function updatePersonalMemory(id: string, updates: Partial<PersonalMemory>) {
    const idx = personalMemories.value.findIndex((m) => m.memory_id === id)
    if (idx >= 0) {
      personalMemories.value[idx] = { ...personalMemories.value[idx], ...updates }
    }
  }

  function archivePersonalMemory(id: string) {
    personalMemories.value = personalMemories.value.filter((m) => m.memory_id !== id)
  }

  function setEpisodes(list: EpisodicMemory[]) {
    episodes.value = list
  }

  function addEpisode(episode: EpisodicMemory) {
    episodes.value = [episode, ...episodes.value]
  }

  function triggerRefresh() {
    refreshToken.value++
  }

  function reset() {
    sessionNotes.value = []
    personalMemories.value = []
    episodes.value = []
    refreshToken.value = 0
  }

  return {
    sessionNotes,
    personalMemories,
    episodes,
    refreshToken,
    addSessionNote,
    clearSessionNotes,
    setPersonalMemories,
    addPersonalMemory,
    updatePersonalMemory,
    archivePersonalMemory,
    setEpisodes,
    addEpisode,
    triggerRefresh,
    reset,
  }
})
