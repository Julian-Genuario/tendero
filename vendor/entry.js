// Only what Tendero's fulfillment board uses. Bryntum keeps its own registry (locales...) on
// globalThis.bryntum, so we add to that object instead of replacing it.
import '@bryntum/scheduler-community/lib/Scheduler/localization/En.js'; // default English texts
import { Scheduler } from '@bryntum/scheduler-community';
import { DateHelper, StringHelper } from '@bryntum/core-community';
globalThis.bryntum = Object.assign(globalThis.bryntum || {}, { Scheduler, DateHelper, StringHelper });
