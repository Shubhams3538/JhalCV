package jobs

import (
	"context"
	"sync"
)

// ReviewStats is the review queue's progress.
type ReviewStats struct {
	Total, Completed, Active, Queued int
}

// ReviewQueue checks jobs independently of company fetching: AISlots
// workers pull batches from a bounded queue, combining ready jobs across
// companies without waiting for a full batch.
type ReviewQueue struct {
	store    *Store
	years    float64
	agents   Asker
	progress func(ReviewStats)
	limit    int // 0 means no limit.
	ctx      context.Context

	jobs chan ReviewJob
	wg   sync.WaitGroup

	mu                              sync.Mutex
	seen                            map[int64]bool
	total, completed, active, fails int
	err                             error
}

// StartReview starts the workers. Call Close when every job has been added.
func StartReview(ctx context.Context, store *Store, years float64, agents Asker, progress func(ReviewStats), limit int) *ReviewQueue {
	q := &ReviewQueue{store: store, years: years, agents: agents, progress: progress, limit: limit, ctx: ctx,
		jobs: make(chan ReviewJob, Batch*AISlots*2), seen: map[int64]bool{}}
	for range AISlots {
		q.wg.Add(1)
		go q.worker()
	}
	return q
}

// Full reports whether the run's review limit is reached.
func (q *ReviewQueue) Full() bool {
	q.mu.Lock()
	defer q.mu.Unlock()
	return q.limit > 0 && q.total >= q.limit
}

// Seen lists job IDs already scheduled in this run.
func (q *ReviewQueue) Seen() map[int64]bool {
	q.mu.Lock()
	defer q.mu.Unlock()
	out := make(map[int64]bool, len(q.seen))
	for k := range q.seen {
		out[k] = true
	}
	return out
}

// Totals returns how many jobs were scheduled and finished.
func (q *ReviewQueue) Totals() (total, completed int) {
	q.mu.Lock()
	defer q.mu.Unlock()
	return q.total, q.completed
}

// Report sends the current progress.
func (q *ReviewQueue) Report() {
	q.mu.Lock()
	defer q.mu.Unlock()
	q.progress(ReviewStats{q.total, q.completed, q.active, q.total - q.completed - q.active - q.fails})
}

// Add schedules jobs, each at most once per run. It blocks while the
// bounded queue is full; fetch workers keep running meanwhile.
func (q *ReviewQueue) Add(jobs []ReviewJob) error {
	for _, j := range jobs {
		q.mu.Lock()
		if q.err != nil {
			err := q.err
			q.mu.Unlock()
			return err
		}
		if q.seen[j.ID] {
			q.mu.Unlock()
			continue
		}
		if q.limit > 0 && q.total >= q.limit {
			q.mu.Unlock()
			break
		}
		q.seen[j.ID] = true
		q.total++
		q.mu.Unlock()
		select {
		case q.jobs <- j:
		case <-q.ctx.Done():
			return q.ctx.Err()
		}
	}
	q.Report()
	return nil
}

func (q *ReviewQueue) worker() {
	defer q.wg.Done()
	for first := range q.jobs {
		batch := []ReviewJob{first}
	fill:
		for len(batch) < Batch {
			select {
			case j, ok := <-q.jobs:
				if !ok {
					break fill
				}
				batch = append(batch, j)
			default:
				break fill
			}
		}
		q.mu.Lock()
		q.active += len(batch)
		failed := q.err
		q.mu.Unlock()
		q.Report()

		var err error
		if failed == nil {
			// At most Batch jobs: Check runs them inline, so no extra AI
			// concurrency is added.
			err = q.store.SetEligibility(q.ctx, Check(q.ctx, batch, q.years, q.agents))
		}
		q.mu.Lock()
		q.active -= len(batch)
		if failed == nil && err == nil {
			q.completed += len(batch)
		} else {
			q.fails += len(batch)
			if q.err == nil {
				q.err = err
			}
		}
		q.mu.Unlock()
		q.Report()
	}
}

// Close waits for in-flight work and returns the first error.
func (q *ReviewQueue) Close() error {
	close(q.jobs)
	q.wg.Wait()
	q.mu.Lock()
	defer q.mu.Unlock()
	return q.err
}
