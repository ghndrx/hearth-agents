// Wikidelve client: deep research service configured via WIKIDELVE_URL env var.
// Provides research, knowledge base search, article management, and quality tools.

const BASE_URL = process.env.WIKIDELVE_URL || '';

interface ResearchJob {
  job_id: string;
  status: 'queued' | 'searching' | 'synthesizing' | 'complete' | 'error' | 'cancelled';
  topic: string;
  articles_created?: number;
  word_count?: number;
}

interface SearchResult {
  slug: string;
  kb: string;
  title: string;
  snippet: string;
  rank?: number;
}

interface Article {
  slug: string;
  title: string;
  kb: string;
  summary: string;
  raw_markdown: string;
  html: string;
  tags: string[];
  word_count: number;
}

async function fetchJSON(path: string, options?: RequestInit): Promise<unknown> {
  const url = `${BASE_URL}${path}`;
  const res = await fetch(url, {
    ...options,
    headers: {
      'Content-Type': 'application/json',
      ...options?.headers,
    },
  });
  if (!res.ok) {
    throw new Error(`Wikidelve ${res.status}: ${await res.text().catch(() => res.statusText)}`);
  }
  return res.json();
}

// -- Research --

export async function startResearch(topic: string): Promise<ResearchJob> {
  return fetchJSON('/api/research', {
    method: 'POST',
    body: JSON.stringify({ topic }),
  }) as Promise<ResearchJob>;
}

export async function pollResearchStatus(jobId: string): Promise<ResearchJob> {
  return fetchJSON(`/api/research/status/${jobId}`) as Promise<ResearchJob>;
}

export async function waitForResearch(jobId: string, timeoutMs = 300_000): Promise<ResearchJob> {
  const start = Date.now();
  while (Date.now() - start < timeoutMs) {
    const job = await pollResearchStatus(jobId);
    if (job.status === 'complete' || job.status === 'error' || job.status === 'cancelled') {
      return job;
    }
    console.log(`[wikidelve] Research "${job.topic}" status: ${job.status}`);
    await new Promise(r => setTimeout(r, 5_000));
  }
  throw new Error(`Research job ${jobId} timed out after ${timeoutMs / 1000}s`);
}

export async function listRecentJobs(): Promise<ResearchJob[]> {
  return fetchJSON('/api/research/jobs') as Promise<ResearchJob[]>;
}

export async function getJobSources(jobId: string): Promise<Array<{ url: string; title: string; word_count: number }>> {
  return fetchJSON(`/api/research/sources/${jobId}`) as Promise<Array<{ url: string; title: string; word_count: number }>>;
}

// -- Search --

export async function search(query: string): Promise<SearchResult[]> {
  return fetchJSON(`/api/search?q=${encodeURIComponent(query)}`) as Promise<SearchResult[]>;
}

export async function hybridSearch(query: string, kb?: string, limit = 15): Promise<SearchResult[]> {
  const params = new URLSearchParams({ q: query, limit: String(limit) });
  if (kb) params.set('kb', kb);
  return fetchJSON(`/api/search/hybrid?${params}`) as Promise<SearchResult[]>;
}

// -- Articles --

export async function getArticle(kb: string, slug: string): Promise<Article> {
  return fetchJSON(`/api/articles/${kb}/${slug}`) as Promise<Article>;
}

export async function listArticles(kb?: string): Promise<Article[]> {
  const params = kb ? `?kb=${encodeURIComponent(kb)}` : '';
  return fetchJSON(`/api/articles${params}`) as Promise<Article[]>;
}

export async function getRelatedArticles(kb: string, slug: string): Promise<Array<{ slug: string; title: string; score: number }>> {
  return fetchJSON(`/api/articles/${kb}/${slug}/related`) as Promise<Array<{ slug: string; title: string; score: number }>>;
}

// -- Quality --

export async function getQualityScores(kb: string): Promise<{ average: number; worst_10: Article[]; best_10: Article[] }> {
  return fetchJSON(`/api/quality/scores/${kb}`) as Promise<{ average: number; worst_10: Article[]; best_10: Article[] }>;
}

export async function enrichArticle(kb: string, slug: string): Promise<{ status: string }> {
  return fetchJSON(`/api/quality/enrich/${kb}/${slug}`, { method: 'POST' }) as Promise<{ status: string }>;
}

// -- Ingest --

export async function ingestYouTube(urls: string[]): Promise<{ jobs: Array<{ job_id: string; url: string }> }> {
  return fetchJSON('/api/media/youtube', {
    method: 'POST',
    body: JSON.stringify({ urls }),
  }) as Promise<{ jobs: Array<{ job_id: string; url: string }> }>;
}

export async function ingestDocument(urls: string[], kb?: string): Promise<{ jobs: Array<{ job_id: string }> }> {
  return fetchJSON('/api/ingest/document', {
    method: 'POST',
    body: JSON.stringify({ urls, kb }),
  }) as Promise<{ jobs: Array<{ job_id: string }> }>;
}

// -- System --

export async function getStatus(): Promise<{
  jobs: { total: number; complete: number; active: number; errors: number; words_generated: number };
  wiki: { total_articles: number; total_words: number };
}> {
  return fetchJSON('/api/status') as Promise<any>;
}

export async function getStats(): Promise<Record<string, { articles: number; words: number }>> {
  return fetchJSON('/api/stats') as Promise<any>;
}

// -- High-level: research a topic and return the article --

export async function researchAndGet(topic: string): Promise<{ job: ResearchJob; articles: Article[] }> {
  console.log(`[wikidelve] Starting research: "${topic}"`);
  const job = await startResearch(topic);
  const completed = await waitForResearch(job.job_id);

  if (completed.status !== 'complete') {
    throw new Error(`Research failed: ${completed.status}`);
  }

  // Search for articles created by this research
  const results = await hybridSearch(topic, undefined, 5);
  const articles: Article[] = [];
  for (const r of results) {
    try {
      const article = await getArticle(r.kb, r.slug);
      articles.push(article);
    } catch {
      // Article might not exist yet
    }
  }

  console.log(`[wikidelve] Research complete: ${completed.articles_created} articles, ${completed.word_count} words`);
  return { job: completed, articles };
}
