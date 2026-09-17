"""Shared request deadlines and bounded, non-queued blocking operations."""
import contextvars
import threading
import time


class RequestTimeout(TimeoutError):
    pass


class RequestBusy(RuntimeError):
    pass


class Budget:
    def __init__(self, seconds=120, *, deadline=None, parent=None):
        self.deadline = min(deadline if deadline is not None else float('inf'), time.monotonic() + seconds)
        self.parent = parent
        self.cancelled = threading.Event()

    def remaining(self, maximum=None):
        if self.parent:
            self.parent.remaining()
        if self.cancelled.is_set() or (self.parent and self.parent.cancelled.is_set()):
            raise RequestTimeout('请求已结束；操作结果须核实')
        left = self.deadline - time.monotonic()
        if left <= 0:
            raise RequestTimeout('本次请求已超过总时间预算')
        return min(left, maximum) if maximum is not None else left


CURRENT = contextvars.ContextVar('sanae_request_budget', default=None)
_SLOTS = threading.BoundedSemaphore(8)


def remaining_timeout(maximum=90):
    budget = CURRENT.get()
    return budget.remaining(maximum) if budget else maximum


def check_active():
    remaining_timeout()


def begin_call(callback, *, budget=None, seconds=60):
    parent = budget or CURRENT.get() or Budget()
    child = Budget(seconds, deadline=parent.deadline, parent=parent)
    child.remaining()
    slots = _SLOTS
    if not slots.acquire(blocking=False):
        raise RequestBusy('前序请求仍在结束，请稍后再试')
    state = {'done': threading.Event(), 'budget': child}
    context = contextvars.copy_context()

    def run():
        token = CURRENT.set(child)
        try:
            child.remaining()
            state['value'] = callback()
        except BaseException as exc:
            state['error'] = exc
        finally:
            CURRENT.reset(token)
            slots.release()
            state['done'].set()
    try:
        threading.Thread(target=lambda: context.run(run), daemon=True, name='sanae-bounded-io').start()
    except BaseException:
        slots.release()
        raise
    return state


def finish_call(state):
    child = state['budget']
    try:
        if not state['done'].wait(child.remaining()):
            raise RequestTimeout('操作超时，结果未知；不自动重复执行')
        child.remaining()
        if 'error' in state:
            raise state['error']
        return state.get('value')
    except BaseException:
        child.cancelled.set()
        raise


def bounded_call(callback, *, budget=None, seconds=60):
    return finish_call(begin_call(callback, budget=budget, seconds=seconds))


def looks_like_plan(answer, question, privileged):
    import re
    if not privileged or not answer or len(answer) > 1000:
        return False
    if re.search(r'拒绝|不能|无法|不允许|不会执行|未授权|未配置|不可用', answer):
        return False
    return bool(re.search(r'让我|我先|我去|接下来|正在|准备', answer)
                and re.search(r'读取|查看|检查|搜索|调用|执行|修改|写入', answer)
                and re.search(r'日志|崩溃|模组|文件|配置|命令|工具|状态', answer)
                and re.search(r'读|查|检查|执行|修改|分析|解释|确认', question))
