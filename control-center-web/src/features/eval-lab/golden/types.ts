import { isApplicationMethod, isApplicationMethodComparison, type ApplicationMethod, type ApplicationMethodComparison, type ApplicationMethodInput } from './application-method';
export type ModelConfig = { provider: string; model: string; thinkingLevel: string; prompt: string; applicationMethod?: ApplicationMethod | ApplicationMethodInput };
export const goldenThinkingLevels = ['off', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max'] as const;
export const isGoldenThinkingLevel = (value: string) => goldenThinkingLevels.some((level) => level === value);
export const isRunnableGoldenModel = (value: ModelConfig) => Boolean(value.provider.trim() && value.model.trim() && isGoldenThinkingLevel(value.thinkingLevel)
  && (!value.applicationMethod || ('body' in value.applicationMethod ? value.applicationMethod.body.trim() : value.applicationMethod.artifactId && value.applicationMethod.artifactRevision > 0)));
export type GoldenSource = { sourceId: string; title: string; kind: 'document' | 'history' | 'failure'; uri: string; text: string };
export type GoldenEvidence = { sourceId: string; quote: string };
export type Verdict = 'pass' | 'fail' | 'uncertain';
export type GoldenSample = {
  sampleId: string; answer: string; category: 'correct' | 'incorrect' | 'boundary';
  humanVerdict: Verdict | null; humanNote: string; labelAuthor?: 'human' | 'agent';
};
export type GoldenCase = {
  caseId: string; question: string; taskType: string; answerable: boolean;
  requiredFacts: string[]; evidence: GoldenEvidence[]; rubric: string[];
  split: 'development' | 'holdout';
  review: { status: 'pending' | 'approved' | 'rejected'; note: string; reviewedAtMs: number | null; author?: 'human' | 'agent' };
  samples: GoldenSample[];
};
export type Judgment = { caseId: string; sampleId: string; verdict: Verdict; reason: string; evidence: GoldenEvidence[] };
export type Calibration = {
  calibrationId: string; suiteRevision: number; judgeConfig: ModelConfig;
  judgeProtocolVersion?: string;
  judgments: Judgment[]; metrics: { total: number; comparable: number; agreement: number | null; falsePasses: number; falseFails: number; uncertain: number };
  ready: boolean; reasons: string[]; createdAtMs: number;
  labelAuthors?: { human: number; agent: number; unrecorded: number }; referenceAuthority?: 'human' | 'agent_assisted' | 'unrecorded';
};
export type GoldenSnapshot = {
  snapshotId: string; suiteId: string; version: number; sourceRevision: number; createdAtMs: number;
  developmentCount: number; holdoutCount: number; judgeConfig: ModelConfig;
  judgeProtocolVersion?: string;
};
export type GoldenJob = {
  jobId: string; kind: 'draft' | 'review' | 'calibrate' | 'experiment';
  state: 'queued' | 'running' | 'completed' | 'failed' | 'cancelled' | 'interrupted';
  progress: string; sessionId: string; error: string; result: Record<string, unknown> | null;
  canReprocess?: boolean;
  canRetryFailedCall?: boolean;
  createdAtMs: number; updatedAtMs: number;
};
export type GoldenSuite = {
  schemaVersion: 'rag-ime.agent-lab-golden-suite.v1'; suiteId: string; title: string; scenario: string;
  revision: number; targetCount: number; sources: GoldenSource[]; cases: GoldenCase[];
  judgeConfig: ModelConfig; calibration: Calibration | null; snapshot: GoldenSnapshot | null;
  jobs: GoldenJob[]; createdAtMs: number; updatedAtMs: number;
  currentJudgeProtocolVersion?: string;
  knowledge?: { indexId: string; corpusHash: string; documentCount: number; chunkCount: number; profile: Record<string, unknown> };
  datasetProvenance?: { kind: string; totalCases: number; selectedCases: number; diagnosticSamples?: string };
};
export type GoldenRead = { ok: true; items: GoldenSuite[]; suite: GoldenSuite | null };
export type GoldenAction = 'create' | 'draft' | 'review' | 'review_case' | 'label_sample' | 'judge_config' | 'calibrate' | 'freeze' | 'experiment' | 'cancel' | 'resume';
export type GoldenCommand = {
  action: GoldenAction; suiteId?: string; expectedRevision: number; clientRequestId: string;
  input: Record<string, import('@/platform/transport').JsonValue>;
};
export type GoldenReceipt = { ok: true; suite: GoldenSuite; job: GoldenJob | null; clientRequestId: string; replayed: boolean };
export type CaseRun = {
  answer: string; status: 'graded' | 'runtime_error';
  judgment: { verdict: Verdict; reason: string; evidence: GoldenEvidence[] };
  requestId: string; sessionId: string; turnId: string;
  retrieval?: { indexId: string; sourceCount: number; contextChars: number; sources: { sourceId: string; chunkId: string; excerpt?: string }[] };
};
export type ExperimentMetrics = { total: number; passed: number; failed: number; uncertain: number; runtimeErrors: number; passRate: number | null };
export type PhaseReport = {
  cases: { caseId: string; question: string; taskType: string; baseline: CaseRun; candidate: CaseRun }[];
  baselineMetrics: ExperimentMetrics; candidateMetrics: ExperimentMetrics;
  baselineUsage?: ExperimentUsage; candidateUsage?: ExperimentUsage; usageScope?: 'answer_calls_only';
  businessCost?: { basis: 'actual' | 'model_catalog_estimate' | 'unavailable'; baselineUsd: number | null; candidateUsd: number | null; deltaUsd: number | null; reductionFraction: number | null; baselineCostPerSuccessUsd: number | null; candidateCostPerSuccessUsd: number | null };
};
export type ExperimentUsage = {
  calls: number; inputTokens: number | null; outputTokens: number | null; totalTokens: number | null;
  costUsd: number | null; knownCostUsd: number | null; pricedCalls: number;
  tokensComplete: boolean; costComplete: boolean; source: 'runtime_receipts';
  estimatedCostUsd?: number | null; knownEstimatedCostUsd?: number | null; estimatedPricedCalls?: number; estimateComplete?: boolean;
};
export type ExperimentResult = {
  knowledge?: GoldenSuite['knowledge'];
  referenceAuthority?: 'human' | 'agent_assisted' | 'unrecorded';
  labelAuthors?: { human: number; agent: number; unrecorded: number } | null;
  schemaVersion: 'rag-ime.agent-lab-golden-experiment.v1'; suiteId: string; snapshotId: string;
  executionMode: 'context_qa' | 'knowledge_qa'; optimizationScope: 'prompt' | 'model' | 'model_and_prompt' | 'repeat' | 'skill' | 'model_and_skill' | 'prompt_and_skill' | 'model_and_prompt_and_skill'; judgeConfig: ModelConfig;
  applicationMethodComparison?: ApplicationMethodComparison;
  baseline: ModelConfig; candidate: ModelConfig; development: PhaseReport; holdout: PhaseReport;
  optimization: { enabled: boolean; maxCandidates: number; selectedCandidateIndex: number; proposals: { candidateIndex: number; modelConfig: ModelConfig; developmentMetrics: ExperimentMetrics; selected: boolean; proposalRequestId: string }[] };
  comparison: { decision: 'improved' | 'no_improvement' | 'inconclusive'; comparable: boolean | number; developmentDelta: number | null; holdoutDelta: number | null; reasons: string[]; sameSnapshot: true; goldenChanged: false; improvementBasis?: 'quality' | 'answer_cost' | 'answer_cost_estimate' | null; groupRegressions?: unknown[] };
  receipts: { requestId: string; stage: string; sessionId: string; turnId: string; usage: unknown; receipt: unknown }[];
  usage: ExperimentUsage;
  validationUse?: { snapshotId: string; ordinal: number; priorStartedRuns: number; priorCompletedRuns: number; reused: boolean; overlappingQuestionCount?: number; totalQuestions?: number; startedAtMs: number; scope: 'local_lab_validation_entry' };
  usageByScope?: Partial<Record<'baselineAnswers' | 'candidateAnswers' | 'judging' | 'optimization' | 'drafting' | 'calibration', ExperimentUsage>>;
};

export const blankModel = (): ModelConfig => ({ provider: '', model: '', thinkingLevel: '', prompt: '' });
export const isActiveJob = (job: GoldenJob) => job.state === 'queued' || job.state === 'running';
export const verdictLabel: Record<Verdict, string> = { pass: '通过', fail: '不通过', uncertain: '无法判定' };
export const splitLabel = { development: '开发题', holdout: '留出题' };
export const categoryLabel = { correct: '正确样例', incorrect: '错误样例', boundary: '边界样例' };
export const jobLabel = { draft: '起草题目', review: 'Agent 核对', calibrate: '校准评审', experiment: '运行实验' };
export const jobStateLabel = { queued: '排队中', running: '进行中', completed: '已完成', failed: '失败', cancelled: '已停止', interrupted: '已中断' };
export const object = (value: unknown): Record<string, unknown> => value !== null && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {};
const strings = (value: unknown): value is string[] => Array.isArray(value) && value.every((item) => typeof item === 'string');
const fields = (value: Record<string, unknown>, keys: string[]) => keys.every((key) => typeof value[key] === 'string');
const number = (value: unknown): value is number => typeof value === 'number' && Number.isFinite(value);
const nullableNumber = (value: unknown) => value === null || number(value);
const verdict = (value: unknown) => value === 'pass' || value === 'fail' || value === 'uncertain';
const model = (value: unknown) => fields(object(value), ['provider', 'model', 'thinkingLevel', 'prompt']);
const evidence = (value: unknown) => Array.isArray(value) && value.every((item) => fields(object(item), ['sourceId', 'quote']));

export function isGoldenJob(value: unknown): value is GoldenJob {
  const job = object(value);
  return fields(job, ['jobId', 'progress', 'sessionId', 'error']) && job.jobId !== ''
    && ['draft', 'review', 'calibrate', 'experiment'].includes(String(job.kind))
    && ['queued', 'running', 'completed', 'failed', 'cancelled', 'interrupted'].includes(String(job.state))
    && (job.canReprocess === undefined || typeof job.canReprocess === 'boolean')
    && (job.canRetryFailedCall === undefined || typeof job.canRetryFailedCall === 'boolean')
    && (job.result === null || (typeof job.result === 'object' && !Array.isArray(job.result)))
    && number(job.createdAtMs) && number(job.updatedAtMs);
}

export function isGoldenSuite(value: unknown): value is GoldenSuite {
  const suite = object(value);
  const calibration = object(suite.calibration);
  const snapshot = object(suite.snapshot);
  return suite.schemaVersion === 'rag-ime.agent-lab-golden-suite.v1'
    && fields(suite, ['suiteId', 'title', 'scenario']) && suite.suiteId !== ''
    && Number.isSafeInteger(suite.revision) && Number(suite.revision) >= 0 && number(suite.targetCount)
    && model(suite.judgeConfig) && number(suite.createdAtMs) && number(suite.updatedAtMs)
    && Array.isArray(suite.sources) && suite.sources.every((source) => {
      const row = object(source);
      return fields(row, ['sourceId', 'title', 'uri', 'text']) && ['document', 'history', 'failure'].includes(String(row.kind));
    })
    && Array.isArray(suite.cases) && suite.cases.every((item) => {
      const row = object(item); const review = object(row.review);
      return fields(row, ['caseId', 'question', 'taskType']) && typeof row.answerable === 'boolean'
        && strings(row.requiredFacts) && strings(row.rubric) && evidence(row.evidence)
        && ['development', 'holdout'].includes(String(row.split))
        && ['pending', 'approved', 'rejected'].includes(String(review.status)) && typeof review.note === 'string'
        && nullableNumber(review.reviewedAtMs) && Array.isArray(row.samples) && row.samples.every((sample) => {
          const value = object(sample);
          return fields(value, ['sampleId', 'answer', 'humanNote']) && ['correct', 'incorrect', 'boundary'].includes(String(value.category))
            && (value.humanVerdict === null || verdict(value.humanVerdict));
        });
    })
    && Array.isArray(suite.jobs) && suite.jobs.every(isGoldenJob)
    && (suite.calibration === null || (fields(calibration, ['calibrationId']) && number(calibration.suiteRevision)
      && model(calibration.judgeConfig) && typeof calibration.ready === 'boolean' && strings(calibration.reasons)
      && Array.isArray(calibration.judgments) && calibration.judgments.every((item) => {
        const row = object(item); return fields(row, ['caseId', 'sampleId', 'reason']) && verdict(row.verdict) && evidence(row.evidence);
      }) && ['total', 'comparable', 'falsePasses', 'falseFails', 'uncertain'].every((key) => number(object(calibration.metrics)[key]))
      && nullableNumber(object(calibration.metrics).agreement) && number(calibration.createdAtMs)))
    && (suite.snapshot === null || (fields(snapshot, ['snapshotId', 'suiteId']) && model(snapshot.judgeConfig)
      && ['version', 'sourceRevision', 'createdAtMs', 'developmentCount', 'holdoutCount'].every((key) => number(snapshot[key]))));
}

export function parseGoldenRead(value: unknown, expectedSuiteId = ''): GoldenRead {
  const read = object(value);
  if (read.ok !== true || !Array.isArray(read.items) || !read.items.every(isGoldenSuite)
    || !(read.suite === null || isGoldenSuite(read.suite))
    || (expectedSuiteId && object(read.suite).suiteId !== expectedSuiteId)) throw new Error('评测集状态不完整，请重新读取。');
  return read as GoldenRead;
}

export function isExperimentResult(value: unknown): value is ExperimentResult {
  const result = object(value); const comparison = object(result.comparison);
  const validation = object(result.validationUse);
  const phase = (value: unknown) => {
    const report = object(value);
    const metrics = (value: unknown) => ['total', 'passed', 'failed', 'uncertain', 'runtimeErrors'].every((key) => number(object(value)[key])) && nullableNumber(object(value).passRate);
    return metrics(report.baselineMetrics) && metrics(report.candidateMetrics) && Array.isArray(report.cases) && report.cases.every((value) => {
      const row = object(value);
      return fields(row, ['caseId', 'question', 'taskType']) && ['baseline', 'candidate'].every((key) => {
        const run = object(row[key]); const judgment = object(run.judgment);
        return fields(run, ['answer', 'requestId', 'sessionId', 'turnId']) && ['graded', 'runtime_error'].includes(String(run.status))
          && verdict(judgment.verdict) && typeof judgment.reason === 'string' && evidence(judgment.evidence);
      });
    });
  };
  return result.schemaVersion === 'rag-ime.agent-lab-golden-experiment.v1' && fields(result, ['suiteId', 'snapshotId'])
    && (result.knowledge === undefined || (fields(object(result.knowledge), ['indexId', 'corpusHash']) && number(object(result.knowledge).documentCount) && number(object(result.knowledge).chunkCount) && typeof object(object(result.knowledge).profile).mode === 'string'))
    && ['context_qa', 'knowledge_qa'].includes(String(result.executionMode)) && ['prompt', 'model', 'model_and_prompt', 'repeat', 'skill', 'model_and_skill', 'prompt_and_skill', 'model_and_prompt_and_skill'].includes(String(result.optimizationScope))
    && (result.applicationMethodComparison === undefined || isApplicationMethodComparison(result.applicationMethodComparison))
    && ['baseline', 'candidate'].every((key) => object(result[key]).applicationMethod === undefined || isApplicationMethod(object(result[key]).applicationMethod))
    && (result.validationUse === undefined || (Number.isSafeInteger(validation.ordinal) && Number(validation.ordinal) > 0 && typeof validation.reused === 'boolean'
      && number(validation.priorStartedRuns) && number(validation.priorCompletedRuns) && number(validation.startedAtMs)
      && (validation.overlappingQuestionCount === undefined || number(validation.overlappingQuestionCount))))
    && model(result.baseline) && model(result.candidate) && model(result.judgeConfig)
    && phase(result.development) && phase(result.holdout)
    && ['improved', 'no_improvement', 'inconclusive'].includes(String(comparison.decision))
    && strings(comparison.reasons) && nullableNumber(comparison.developmentDelta) && nullableNumber(comparison.holdoutDelta)
    && comparison.sameSnapshot === true && comparison.goldenChanged === false
    && Array.isArray(result.receipts) && result.receipts.every((receipt) => fields(object(receipt), ['requestId', 'stage', 'sessionId', 'turnId']))
    && Array.isArray(object(result.optimization).proposals)
    && number(object(result.usage).calls);
}
