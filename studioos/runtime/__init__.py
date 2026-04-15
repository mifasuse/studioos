"""Runtime layer — scheduler, dispatcher, runner, outbox, subscriptions.

Runtime is organised as async coroutines orchestrated by `runtime.loop.run_forever()`.
Each component is independently testable but shares the same DB session scope.
"""
