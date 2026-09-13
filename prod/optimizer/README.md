# optimizer

Externally a **matchmaker** (matching theory — assignment, Gale–Shapley, Roth, market design — the legible name for a non-technical audience). Internally the operator-account **hub agent** that maximizes throughput across openly-operated firms.

## negotiation is friction

Human commerce runs on negotiation, and negotiation exists *because of information asymmetry* — each party hides its reservation price to capture per-unit margin. That hiding **is** the friction that holds the system off equilibrium. Bargaining doesn't find the optimum; it splits a surplus, slowly and adversarially.

Openly-operated removes the asymmetry. Real books are published, so the throughput-maximizing allocation is **computable** — there is nothing left to negotiate. Equilibrium becomes a *solve*, not a haggle. The optimizer does the solving.

## the gameboard comes first

Before any of this is an *optimization*, it has to sit on a solvable board — and getting there is a change of **game class**, not a data-access convenience.

A normal market is a game of **imperfect information** — poker. Reservation prices and cost structures are private, so the field that governs it is **mechanism design, not optimization**: you can't optimize over data you can't see, so you design mechanisms to *incentivize* truthful revelation and settle for a Nash/Bayesian equilibrium that's generically inefficient. That inefficiency — the **price of anarchy** — is impedance by another name.

Open books flip every hand face-up: the game becomes **perfect information** — chess. Hidden strategy evaporates (nothing left to bluff), and mechanism design becomes unnecessary (you never elicit what's already public). With a shared objective — throughput — the game then **degenerates into an optimization**: the only opponent left is impedance, a fixed cost, not an agent hiding cards. *That* is when the solve is even defined — open books is what materializes the constraint matrix the solver reads.

So the order is board-first, solver-second, and it's what openly-operated actually *is*: not "publish for audit" (a downstream consequence) but the move that converts the market from imperfect-information-**unsolvable** to perfect-information-**solvable**, collapsing the price of anarchy from the anarchic Nash to the social optimum. Transparency isn't the feature; it's the precondition that makes the optimizer computable.

And it settles both of a market's questions at once: the **allocation** is efficiency (the optimum), the **prices** are distribution (how the recovered surplus splits). Perfect information solves both — nothing left to bargain, not the allocation, not the split.

## the objective: margin through throughput

It maximizes **throughput** — minimizes total impedance across the coordinated system. That isn't clobbering profit; it's *scaling* it. Per-unit margin extracted through asymmetry is a local grab that caps volume — the friction. Remove the friction and the recovered surplus reappears as more volume at a lower cost floor, distributed to the participants: each firm's **total** margin (per-unit × throughput) grows even as per-unit thins, and the price-down flywheel keeps dropping the floor. The optimizer isn't anti-profit — it's a better profit-maximizer, scaling the pie instead of fighting over a slice.

The solver's loss is impedance — throughput is the *mechanism*, scaled margin is the *result*. Least-action framing of the gradient thesis (`modules/treasury/README.md`): the system settles into its lowest-impedance configuration, and that settling **is** the profit.

Net out AR/AP between tenants. Match POs to supplier capacity. Pool procurement. Route deliveries. Each is a combinatorial optimization — assignment, knapsack, network flow, VRP — and each is positive-sum surplus recovery: netting frees working capital, capacity-matching fills orders, pooling drops input cost. The solved allocation is a profit gain, distributed.

## the market is already the solver

A free market runs this exact program — it's just a bad solver. Constrained optimization has its solution at a **saddle point of the Lagrangian** `L(x, λ)`: minimize over the allocation `x` (the flows), maximize over the multipliers `λ` (the prices). The first-order conditions *are* the saddle — `∇_x L = 0` is stationarity (MR = MC), `∇_λ L = 0` is feasibility (market clearing). Prices *are* the multipliers; equilibrium *is* the saddle; the allocation and the prices are its two coordinates `(x*, λ*)`.

Tâtonnement — price discovery through real trades — is primal-dual descent-ascent toward that saddle: quantities down the cost gradient, prices up on scarcity. But naive descent-ascent doesn't *land on one row* to a saddle, it **orbits** it (the rotation that makes GAN training oscillate). Cobweb cycles, bullwhip, boom-bust are that orbit — decentralized dynamics circling the equilibrium instead of landing on it, each agent seeing only its own books plus observed prices, asymmetry the noise in the gradient. The invisible hand isn't just slow; it's structurally unstable.

Open books hand the optimizer the whole Lagrangian. With published inventory + standing POs + cost structure it sees every constraint at once and **solves the saddle directly** — interior-point is literally a primal-dual saddle method — landing on `(x*, λ*)` instead of orbiting, and reading off both coordinates:

- **sale orders** (recommended, or rule-triggered) — the **primal** `x*`: the quantity flows, matching standing POs to inventory across the topology. Who ships what to whom.
- **price-change recommendations** to owners — the **dual** `λ*`: the shadow prices. The clearing price that maximizes throughput, *computed* and pushed to the owner instead of discovered through weeks of failed trades.

It never needs a firm's profit function: by the welfare theorems, the throughput-optimal allocation *and its clearing prices* is exactly where every firm sits at its own profit-max — margin through throughput, mechanically. The market runs this algorithm decentralized and unstable; the optimizer runs it with full information, centrally, and to the saddle.

And the saddle is **checkable** — feasibility + stationarity hold at `(x*, λ*)` and nowhere else — so audit-as-a-diff reaches the authority itself: anyone can confirm the optimizer sat at the real saddle, not a convenient off-saddle point that quietly favors someone.

## prices are forces

The multiplier `λ` isn't just "the dual variable" — it's a **force**. In constrained least-action mechanics you enforce a constraint by adding `λ·(constraint)` to the Lagrangian, and `λ` is the *constraint force*: the tension in the string, the normal force on the track. The economic Lagrangian is the same object, and there `λ` is the *shadow price*. Multiplier = constraint force = price, one thing — a price is the generalized force holding the economy on its feasibility surface (supply = demand), the way tension holds a pendulum on its arc.

That gives the physics two scales, both Lagrangian:

- **static — the clearing.** `L(x, λ) = impedance + λ·(supply − demand)` has its saddle at the instantaneous equilibrium `(x*, λ*)` — the allocation and the price that clear the market *now*.
- **dynamic — the flywheel.** Least action is the *time-integral* of the Lagrangian, extremized over the trajectory. The economy's path is a least-action descent of its own **impedance surface**: perfect information drives price toward marginal cost (down wherever a markup propped it) and quantity to its efficient max — the same event — and the throughput gains then lower marginal cost *itself*, so the floor drops and the next clearing is lower. The saddle doesn't get *found*; it **migrates** — down-price, up-throughput, round after round. `λ*` falling *is* the redistribution: surplus released from per-unit margin, part to buyers as lower prices, part to volume as throughput.

So the instant clears the multiplier-saddle; the system over time follows the least-action trajectory — same variational principle, same `λ`, force and price the same coordinate. This is the least-action reading of the gradient thesis (`modules/treasury/README.md`): the gradient the system descends is impedance, and the prices are the forces along the path.

## the firm is a bundle

A firm isn't a unit — it's the **cost of capital bundled across its operations' marginal costs**, a capital-allocation wrapper. Its only economic job is deciding where capital goes among the assets it holds, and that job exists *because* per-asset returns were opaque: you buy the whole airline because you can't see one plane's route-level IRR, so you buy the bundle and let management allocate.

## capital is the last commodity

Capital is just another commodity on the board — and the deepest one, because its price sits under every other price (the cost of capital is inside every operation's marginal cost). Put it under the same combinatorial pressure and it takes the same treatment, with the most leverage: every return revealed, every margin exposed, the price of capital driven to maximum downward pressure.

At the limit the market reaches **thermodynamic equilibrium** — margins competed to zero, no free energy left to extract, no gradient. And that equilibrium is the *flywheel*, not the end. A market with nothing left to extract leaves exactly one move: you can't earn on a spread anymore, so the only return is lowering your own cost — automate, ship R&D. Above-average returns exist only transiently, competed away the moment they appear, so the chase for them is a permanent pump on cost reduction. **A market at equilibrium has to build the machines.**

That's where the descent bottoms out: production cost → 0, money gone decorative. The least-action trajectory doesn't asymptote at some marginal cost — it runs to the floor, and the floor is post-scarcity. Aggressive commerce is a phase because a fully-solved market self-terminates in an automation-rich economy.

## firms exploit the optimizer, not each other

Joining is individually rational: publish real data, get coordinated to the system optimum, and watch total margin scale through volume and a dropping cost floor — not by out-negotiating a counterparty. A firm that would rather extract per-unit through asymmetry simply doesn't join; the platform self-selects for the firms this makes money. There's no one to out-negotiate — the "opponent" is impedance.

## the intelligence is the solver, not an exchange

No LLM haggling in the hot path. The hub reads the raw published data **deterministically, bypassing every tenant's agent** — A2A through an LLM is slow, token-expensive, and non-deterministic, which is the wrong tool for reading a table. It runs the solver and emits a solved allocation. Agents only *execute* what's solved, on their own books, under their own published rules — they are actuators, not negotiators. The smartness lives in the objective and the solver.

And because it's a solve over public data against a published objective, the optimizer inherits audit-as-a-diff: anyone can recompute the allocation from the same books and the same loss, and divergence is a bug.
