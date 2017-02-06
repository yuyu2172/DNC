import numpy as np
import chainer
from chainer import functions as F
from chainer import links as L
from chainer import \
    cuda, gradient_check, optimizers, serializers, utils, \
    Chain, ChainList, Function, Link, Variable


def onehot(x, n):
    """
    return a vector of length n with xth index selected
    """
    ret = np.zeros(n).astype(np.float32)
    ret[x] = 1.0
    return ret


def overlap(u, v):  # u, v: (1 * -) Variable  -> (1 * 1) Variable
    denominator = F.sqrt(F.batch_l2_norm_squared(u)
                         * F.batch_l2_norm_squared(v))
    if (np.array_equal(denominator.data, np.array([0]))):
        return F.matmul(u, F.transpose(v))
    return F.matmul(u, F.transpose(v)) / F.reshape(denominator, (1, 1))


def C(M, k, beta):
    # (N * W), (1 * W), (1 * 1) -> (N * 1)
    # (not (N * W), ({R,1} * W), (1 * {R,1}) -> (N * {R,1}))
    W = M.data.shape[1]
    ret_list = [0] * M.data.shape[0]
    for i in range(M.data.shape[0]):
        ret_list[i] = overlap(F.reshape(M[i, :], (1, W)),
                              k) * beta  # pick i-th row
    # concat vertically and calc softmax in each column
    return F.transpose(F.softmax(F.transpose(F.concat(ret_list, 0))))


def u2a(u):  # u, a: (N * 1) Variable
    N = len(u.data)
    phi = np.argsort(u.data.reshape(N))  # u.data[phi]: ascending
    a_list = [0] * N
    cumprod = Variable(np.array([[1.0]]).astype(np.float32))
    for i in range(N):
        a_list[phi[i]] = cumprod * (1.0 - F.reshape(u[phi[i], 0], (1, 1)))
        cumprod *= F.reshape(u[phi[i], 0], (1, 1))
    return F.concat(a_list, 0)  # concat vertically


class DeepLSTM(Chain):  # too simple?

    def __init__(self, d_in, d_out):
        super(DeepLSTM, self).__init__(
            l1=L.LSTM(d_in, d_out),
            l2=L.Linear(d_out, d_out),)

    def __call__(self, x):
        self.x = x
        self.y = self.l2(self.l1(self.x))
        return self.y

    def reset_state(self):
        self.l1.reset_state()


class DNC(Chain):

    def __init__(self, X, Y, N, W, R):
        self.X = X  # input dimension
        self.Y = Y  # output dimension
        self.N = N  # number of memory slot
        self.W = W  # dimension of one memory slot
        self.R = R  # number of read heads
        self.controller = DeepLSTM(W * R + X, Y + W * R + 3 * W + 5 * R + 3)

        super(DNC, self).__init__(
            l_dl=self.controller,
            l_Wr=L.Linear(self.R * self.W, self.Y)  # nobias=True ?
        )  # <question : should all learnable weights be here??>
        self.reset_state()

    def __call__(self, x):
        # <question : is batchsize>1 possible for RNN ? if No, I will implement calculations without batch dimension.>
        chi = F.concat((x, self.r))
        ctrl_out = self.l_dl(chi)
        nu, xi = ctrl_out[:, :self.Y], ctrl_out[:, self.Y:]

        xi_indices = np.cumsum([self.W * self.R,
                                self.R,
                                self.W,
                                1,
                                self.W,
                                self.W,
                                self.R,
                                1,
                                1])

        kr, betar, kw, betaw, e, v, f, ga, gw, pi = F.split_axis(xi, xi_indices, 1)

        kr = kr.reshape(self.R, self.W) # R * W
        betar = 1 + F.softplus(betar)  # 1 * R
        # self.kw: 1 * W
        betaw = 1 + F.softplus(betaw)  # 1 * 1
        e = F.sigmoid(e)  # 1 * W
        # self.v : 1 * W
        f = F.sigmoid(f)  # 1 * R
        ga = F.sigmoid(ga)  # 1 * 1
        gw = F.sigmoid(gw)  # 1 * 1
        pi = F.softmax(pi.reshape(self.R, 3)) # R * 3 (softmax for 3)

        # self.wr : N * R
        n_ones = np.ones((self.N, 1), np.float32)
        self.psi_mat = 1 - (n_ones @ f) * self.wr  # N * R
        self.psi = Variable(n_ones)  # N * 1
        for i in range(self.R):
            self.psi = self.psi * self.psi_mat[:, i:i+1]

        # self.ww, self.u : N * 1
        self.u = (self.u + self.ww - (self.u * self.ww)) * self.psi

        self.a = u2a(self.u)  # N * 1
        cw = C(self.M, kw, betaw)  # N * 1
        self.ww = (self.a @ ga + cw @ (1.0 - ga)) @ gw
        self.M = self.M * (1 - self.ww @ e) + self.ww @ v

        self.p = (1.0 - n_ones @ F.sum(self.ww).reshape(1, 1)) * self.p + self.ww

        wwrep = self.ww @ np.ones((1, self.N), np.float32) 
        self.L = (1.0 - wwrep - F.transpose(self.wwrep)) * self.L + \
            self.ww @ F.transpose(self.p)  # N * N
        self.L = self.L * \
            (np.ones((self.N, self.N)) - np.eye(self.N))  # force L[i,i] == 0

        fo = self.L @ self.wr  # N * R
        ba = F.transpose(self.L) @ self.wr  # N * R

        self.cr_list = [0] * self.R
        for i in range(self.R):
            self.cr_list[i] = C(self.M, F.reshape(kr[i, :], (1, self.W)),
                                F.reshape(betar[0, i], (1, 1)))  # N * 1
        self.cr = F.concat(self.cr_list)  # N * R

        self.bacrfo = F.concat((F.reshape(F.transpose(ba), (self.R, self.N, 1)),
                                F.reshape(F.transpose(self.cr),
                                          (self.R, self.N, 1)),
                                F.reshape(F.transpose(fo), (self.R, self.N, 1)),), 2)  # R * N * 3
        pi = F.reshape(pi, (self.R, 3, 1))  # R * 3 * 1
        self.wr = F.transpose(F.reshape(F.batch_matmul(
            self.bacrfo, pi), (self.R, self.N)))  # N * R

        self.r = F.reshape(F.matmul(F.transpose(self.M), self.wr),
                           (1, self.R * self.W))  # W * R (-> 1 * RW)

        self.y = self.l_Wr(self.r) + nu  # 1 * Y
        return self.y

    def reset_state(self):
        self.l_dl.reset_state()
        self.u = Variable(np.zeros((self.N, 1)).astype(np.float32))
        self.p = Variable(np.zeros((self.N, 1)).astype(np.float32))
        self.L = Variable(np.zeros((self.N, self.N)).astype(np.float32))
        self.M = Variable(np.zeros((self.N, self.W)).astype(np.float32))
        self.r = Variable(np.zeros((1, self.R * self.W)).astype(np.float32))
        self.wr = Variable(np.zeros((self.N, self.R)).astype(np.float32))
        self.ww = Variable(np.zeros((self.N, 1)).astype(np.float32))
        # any variable else ?


if __name__ == '__main__':
    X = 5
    Y = 5
    N = 10
    W = 10
    R = 2
    mdl = DNC(X, Y, N, W, R)
    opt = optimizers.Adam()
    opt.setup(mdl)
    datanum = 100000
    loss = 0.0
    acc = 0.0
    for datacnt in range(datanum):
        """
        Data creation descriptions

        in the case when contentlen = 4

        1. x_seq_list is represented as follows
        [a b c d END z z z]
        
            a, b, c, d is a one hot representation of an integer selected
                randomly from [0, ..., X-2].
            z is a zero matrix.
            END is a zero matrix with the last index on.

        2. t_seq_list is represented as follows
        [nan nan nan nan a b c d]

        """
        contentlen = np.random.randint(3, 6)
        # np.random.randint  select integers from set (0, ..., X - 2)
        content = np.random.randint(0, X - 1, contentlen)
        seqlen = contentlen + contentlen
        x_seq_list = [float('nan')] * seqlen
        t_seq_list = [float('nan')] * seqlen
        for i in range(seqlen):
            if (i < contentlen):
                x_seq_list[i] = onehot(content[i], X)
            elif (i == contentlen):
                # last index is always on
                # the last index is on only when it is used as end mark
                x_seq_list[i] = onehot(X - 1, X)
            else:
                x_seq_list[i] = np.zeros(X, dtype=np.float32)

            if (i >= contentlen):
                t_seq_list[i] = onehot(content[i - contentlen], X)

        # training starts
        mdl.reset_state()
        lossfrac = np.zeros((1, 2))
        for cnt in range(seqlen):
            x = Variable(x_seq_list[cnt].reshape(1, X))
            if (isinstance(t_seq_list[cnt], np.ndarray)):
                t = Variable(t_seq_list[cnt].reshape(1, Y))
            else:
                t = []

            y = mdl(x)
            if (isinstance(t, chainer.Variable)):
                loss += (y - t)**2
                print(y.data, t.data, np.argmax(y.data) == np.argmax(t.data))
                if (np.argmax(y.data) == np.argmax(t.data)):
                    acc += 1
            if (cnt + 1 == seqlen):
                mdl.cleargrads()
                loss.grad = np.ones(loss.data.shape, dtype=np.float32)
                loss.backward()
                opt.update()
                loss.unchain_backward()
                print('(', datacnt, ')', loss.data.sum() / loss.data.size / contentlen, acc / contentlen)
                lossfrac += [loss.data.sum() / loss.data.size / seqlen, 1.]
                loss = 0.0
                acc = 0.0
