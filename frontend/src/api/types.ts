// Friendly names for the generated C4 types. Shapes come only from schema.d.ts (the contract).
import type { components, operations, paths } from './schema';

type S = components['schemas'];

export type Paths = paths;
export type Operations = operations;

export type ApiErrorBody = S['Error'];
export type ErrorCode = S['Error']['error']['code'];
export type Link = S['Link'];

export type Credential = S['Credential'];
export type CredentialCreate = S['CredentialCreate'];
export type CredentialUpdate = S['CredentialUpdate'];
export type VerifyState = S['VerifyState'];
export type VerifyResult = S['VerifyResult'];
export type ProbeResult = S['ProbeResult'];

export type VlmBackend = S['VlmBackend'];
export type VlmBackendCreate = S['VlmBackendCreate'];
export type VlmBackendUpdate = S['VlmBackendUpdate'];
export type VlmModel = S['VlmModel'];
export type VlmModelCreate = S['VlmModelCreate'];
export type VlmModelPatch = S['VlmModelPatch'];
export type ReasoningEffort = S['ReasoningEffort'];
export type ReasoningLevel = Exclude<ReasoningEffort, null>;

export type ModuleRegistry = S['ModuleRegistry'];
export type ModuleSpec = ModuleRegistry['modules'][number];
export type ModuleTableSpec = ModuleSpec['tables'][number];
export type StageId = ModuleSpec['stage'];

export type DatasetFormat = S['DatasetFormat'];
export type DatasetItem = S['DatasetItem'];
export type DatasetDetail = S['DatasetDetail'];
export type DatasetCreate = S['DatasetCreate'];
export type DatasetPatch = S['DatasetPatch'];
export type DatasetCheck = S['DatasetCheck'];
export type SourceChange = S['SourceChange'];
export type BrowsedDataset = S['BrowsedDataset'];
export type EpisodePreview = S['EpisodePreview'];
export type InputRef = S['InputRef'];
export type InputSpec = S['InputSpec'];
export type OutputRef = S['OutputRef'];
export type InputSource = InputRef['source'];

export type PreflightRequest = S['PreflightRequest'];
export type PreflightResponse = S['PreflightResponse'];
export type PreflightResult = S['preflight.schema'];
export type Upload = S['Upload'];
export type UploadKind = S['UploadKind'];
export type UploadIssue = S['UploadIssue'];
export type ModuleAvailability = S['module_availability'];
export type Availability = ModuleAvailability['availability'];
export type DeliveryProbeRequest = S['DeliveryProbeRequest'];

export type Overview = S['Overview'];

export type TaskState = S['TaskState'];
export type TaskRef = S['TaskRef'];
export type EpisodeSelector = S['EpisodeSelector'];
export type ModuleChoice = S['ModuleChoice'];
export type VlmChoice = S['VlmChoice'];
export type TaskParams = S['TaskParams'];
export type TaskCreate = S['TaskCreate'];
export type TaskBatchCreate = S['TaskBatchCreate'];
export type TaskCreated = S['TaskCreated'];
export type TaskPatch = S['TaskPatch'];
export type StageProgress = S['StageProgress'];
export type ModuleState = S['ModuleState'];
export type Summary = S['Summary'];
export type UsageTotals = S['UsageTotals'];
export type TaskListItem = S['TaskListItem'];
export type Task = S['Task'];
export type Subtask = S['Subtask'];
export type SubtaskCreated = S['SubtaskCreated'];
export type TimelineEntry = S['TimelineEntry'];
export type LogLine = S['LogLine'];
export type UsageRow = S['UsageRow'];
export type UsageReport = S['UsageReport'];
export type RepreflightResult = S['RepreflightResult'];
export type Incompatibility = RepreflightResult['incompatibilities'][number];
export type Plan = S['plan.schema'];
export type PlanStage = S['stage'];

export type Report = S['report.schema'];
export type ReportModuleSection = S['module_section'];
export type ReportResponse = operations['getReport']['responses'][200]['content']['application/json'];
export type ReportTablePage = operations['getReportTable']['responses'][200]['content']['application/json'];
export type EpisodeView = S['EpisodeView'];
export type PipelineEpisode = S['PipelineEpisode'];
export type ResultRecord = S['result-record.schema'];
export type Perf = S['Perf'];
export type AdjudicationQuestion = S['AdjudicationQuestion'];
export type AdjudicationCard = S['AdjudicationCard'];
export type AdjudicationCounts = S['AdjudicationCounts'];
export type AdjudicationLine = AdjudicationQuestion['line'];
/** A kind of question from the registry's review_lines catalog (D43). */
export type ReviewLine = S['ReviewLine'];
export type DecisionInput = S['DecisionInput'];
export type DecisionValue = DecisionInput['decision'];
export type Decision = S['Decision'];
export type AdjudicationPage = operations['listAdjudication']['responses'][200]['content']['application/json'];
export type SignedUrl = S['SignedUrl'];

export type SseState = S['SseState'];
export type SseProgress = S['SseProgress'];
export type SseLog = S['SseLog'];
export type SseUsage = S['SseUsage'];
export type SseDone = S['SseDone'];

export type TaskListPage = operations['listTasks']['responses'][200]['content']['application/json'];
export type DatasetListPage = operations['listDatasets']['responses'][200]['content']['application/json'];
export type LogPage = operations['getTaskLogs']['responses'][200]['content']['application/json'];
export type EpisodePreviewPage = operations['listDatasetEpisodes']['responses'][200]['content']['application/json'];
export type BrowsePage = operations['browseDatasets']['responses'][200]['content']['application/json'];
export type RefreshModelsResult = operations['refreshVlmModels']['responses'][200]['content']['application/json'];
export type PurgeResult = operations['purgeTaskArtifacts']['responses'][202]['content']['application/json'];

/** Task states that never change again on their own (the list and overview stop polling). */
export const TERMINAL_STATES: readonly TaskState[] = ['stopped', 'succeeded', 'completed_with_errors', 'failed'];

export function isTerminal(state: TaskState): boolean {
  return TERMINAL_STATES.includes(state);
}
