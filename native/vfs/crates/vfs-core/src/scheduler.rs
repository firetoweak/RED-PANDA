//! Batcher-owned task handles. Cancellation of an observer never detaches them.
use crate::error::Result;
use parking_lot::Mutex;
use std::{
    collections::VecDeque,
    future::Future,
    pin::Pin,
    task::{Context, Poll, Waker},
};
use tokio::{
    runtime::Handle,
    task::{JoinError, JoinHandle},
};

#[derive(Default)]
pub(crate) struct DrainScheduler {
    state: Mutex<State>,
}

#[derive(Default)]
struct State {
    tasks: Vec<JoinHandle<Result<()>>>,
    completed: VecDeque<std::result::Result<Result<()>, JoinError>>,
}

impl DrainScheduler {
    pub fn spawn(&self, runtime: &Handle, task: impl Future<Output = Result<()>> + Send + 'static) {
        self.state.lock().tasks.push(runtime.spawn(task));
    }

    pub fn observe_completed(&self) -> Result<()> {
        loop {
            let result = {
                let mut state = self.state.lock();
                Self::poll_tasks(&mut state, &mut Context::from_waker(Waker::noop()), true);
                state.completed.pop_front()
            };
            match result {
                None => return Ok(()),
                Some(Ok(result)) => result?,
                Some(Err(error)) if error.is_cancelled() => {}
                Some(Err(error)) => std::panic::resume_unwind(error.into_panic()),
            }
        }
    }

    // All handles stay owned here across Pending, including canceled stop callers.
    pub async fn stop(&self) {
        for task in &self.state.lock().tasks {
            task.abort();
        }
        self.wait().await;
    }

    async fn wait(&self) {
        std::future::poll_fn(|context| {
            let mut state = self.state.lock();
            Self::poll_tasks(&mut state, context, false);
            if state.tasks.is_empty() {
                Poll::Ready(())
            } else {
                Poll::Pending
            }
        })
        .await;
    }

    fn poll_tasks(state: &mut State, context: &mut Context<'_>, only_finished: bool) {
        let mut i = 0;
        while i < state.tasks.len() {
            if only_finished && !state.tasks[i].is_finished() {
                i += 1;
                continue;
            }
            match Pin::new(&mut state.tasks[i]).poll(context) {
                Poll::Pending => i += 1,
                Poll::Ready(result) => {
                    drop(state.tasks.swap_remove(i));
                    state.completed.push_back(result);
                }
            }
        }
    }
}
